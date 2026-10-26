"""Offline dependency inventory and operator-imported advisory evidence.

No package manager, resolver, HTTP client, or project code runs here. This is an
inventory format, not a standards-certified SBOM. Exact imported versions are
matched literally; reviewed v2 advisory ranges use bounded ecosystem adapters.
Manifest ranges and environment markers remain unresolved.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from local_coder.file_lock import exclusive_lock
from local_coder.memory.context_store import _clean
from local_coder.statedir import state_path
from local_coder.tools.scan_scope import IGNORED
from local_coder.workspace import Workspace

_MAX_BYTES = 4_000_000
_MAX_ADVISORIES = 5000
_CACHE = '.local-coder/security/advisories.json'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z')
_NPM = re.compile(r'(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]{0,199}\Z')
_VERSION = re.compile(r'[0-9][A-Za-z0-9.!+_-]*(?:\.[A-Za-z0-9!+_-]+)*\Z')
_ID = re.compile(r'(?:CVE-\d{4}-\d{4,}|GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4})\Z')
_MANIFESTS = {'package-lock.json', 'npm-shrinkwrap.json', 'pyproject.toml', 'requirements.txt'}
_UNSUPPORTED = {'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'uv.lock', 'Pipfile.lock', 'Cargo.lock', 'go.sum', 'packages.lock.json', 'Gemfile.lock'}


# Three-valued evaluation: unsupported syntax is never a negative result.
def _npm_version(value):
    match = re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?', value)
    if not match:
        raise ValueError('Unsupported semver')
    major, minor, patch, pre, build = match.groups()
    if build and any(not part for part in build.split('.')):
        raise ValueError('Invalid semver build identifiers')
    identifiers = pre.split('.') if pre else []
    if any(not part or (part.isdigit() and len(part) > 1 and part.startswith('0')) for part in identifiers):
        raise ValueError('Invalid semver prerelease')
    return (int(major), int(minor), int(patch), (1,) if not pre else (0, tuple((0, int(part)) if part.isdigit() else (1, part) for part in identifiers)))


def _version(ecosystem, value):
    if ecosystem == 'npm':
        return _npm_version(value)
    # Optional adapter: packaging is not a runtime requirement of this project.
    from packaging.version import Version
    return Version(value)


def evaluate_range(ecosystem, version, expression):
    """Bounded npm comparator sets/caret/tilde and optional PEP 440 adapter."""
    if not isinstance(expression, str) or not 0 < len(expression) <= 200:
        return None
    try:
        current = _version(ecosystem, version)
        if ecosystem == 'pypi':
            from packaging.specifiers import SpecifierSet
            return SpecifierSet(expression).contains(current, prereleases=True)
        alternatives = expression.split('||')
        if len(alternatives) > 20:
            return None
        results = []
        for alternative in alternatives:
            terms = alternative.replace(',', ' ').strip().split()
            if not terms or len(terms) > 20:
                return None
            comparisons, prerelease_bases = [], set()
            for term in terms:
                match = re.fullmatch(r'(<=|>=|<|>|=|\^|~)?(.+)', term)
                operator, text = match.groups()
                operator = operator or '='
                bound = _npm_version(text)
                if bound[3][0] == 0:
                    prerelease_bases.add(bound[:3])
                if operator in {'^', '~'}:
                    major, minor, patch = bound[:3]
                    upper = (major + 1, 0, 0) if operator == '^' and major else (major, minor + 1, 0) if operator == '~' or minor else (0, 0, patch + 1)
                    comparisons.extend([current >= bound, current < (*upper, (0, ((0, 0),)))])
                else:
                    comparisons.append({'=': current == bound, '<': current < bound, '>': current > bound, '<=': current <= bound, '>=': current >= bound}[operator])
            results.append(all(comparisons) and (current[3][0] == 1 or current[:3] in prerelease_bases))
        return any(results)
    except (ValueError, TypeError, ImportError):
        return None


def _event_range(ecosystem, version, range_):
    """OSV intervals use ecosystem ordering, including prereleases."""
    if range_.get('type') != ('SEMVER' if ecosystem == 'npm' else 'ECOSYSTEM'):
        return None
    try:
        current = _version(ecosystem, version)
        events = range_.get('events')
        if not isinstance(events, list) or not 1 <= len(events) <= 200:
            return None
        intervals, start, open_interval, limits = [], None, False, []
        modes = set()
        for event in events:
            if not isinstance(event, dict) or len(event) != 1:
                return None
            kind, value = next(iter(event.items()))
            if kind not in {'introduced', 'fixed', 'last_affected', 'limit'} or not isinstance(value, str) or len(value) > 120:
                return None
            point = None if value == '0' and kind == 'introduced' or value == '*' and kind == 'limit' else _version(ecosystem, value)
            if kind == 'limit':
                if point is not None:
                    limits.append(point)
            elif kind == 'introduced':
                if open_interval:
                    return None
                start, open_interval = point, True
            else:
                modes.add(kind)
                if not open_interval or start is not None and (point < start or point == start and kind != 'last_affected'):
                    return None
                intervals.append((start, point, kind == 'last_affected'))
                open_interval = False
        if len(modes) > 1 or not any('introduced' in event for event in events):
            return None
        if open_interval:
            intervals.append((start, None, False))
        ordered = sorted(intervals, key=lambda item: (item[0] is not None, item[0]))
        for left, right in zip(ordered, ordered[1:]):
            if left[1] is None or right[0] is None or left[1] > right[0] or left[1] == right[0] and left[2]:
                return None
        return any((low is None or current >= low) and (high is None or current < high or inclusive and current == high) for low, high, inclusive in intervals) and all(current < limit for limit in limits)
    except (ValueError, TypeError, ImportError):
        return None


def advisory_match(item, version):
    if item.get('withdrawn'):
        return False, 'withdrawn', []
    exact = version in item['versions']
    evaluated = []
    for range_ in item['affected_ranges']:
        evaluated.append((evaluate_range(item['ecosystem'], version, range_) if isinstance(range_, str) else _event_range(item['ecosystem'], version, range_)) if item.get('record_version') == 2 else None)
    unresolved = ['Unsupported affected range or unavailable version adapter'] if None in evaluated else []
    return exact or True in evaluated, 'exact_imported_version' if exact else 'ecosystem_range', unresolved


def _normalize_advisory_id(value):
    if value.upper().startswith(('CVE-', 'PYSEC-')):
        return value.upper()
    if value.lower().startswith('ghsa-'):
        return 'GHSA-' + value[5:].lower()
    return value


def _osv_records(raw):
    """Convert a bounded OSV record; retain aliases, version and source provenance."""
    identifier = raw.get('id')
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', identifier):
        raise ValueError('Invalid OSV identifier')
    affected = raw.get('affected', [])
    if not isinstance(affected, list) or len(affected) > 100:
        raise ValueError('Invalid OSV affected packages')
    aliases = raw.get('aliases', [])
    if not isinstance(aliases, list) or len(aliases) > 100 or any(not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', alias) for alias in aliases):
        raise ValueError('Invalid OSV aliases')
    result = []
    for affected_ in affected:
        if not isinstance(affected_, dict) or not isinstance(affected_.get('package'), dict):
            raise ValueError('Invalid OSV package')
        package = affected_['package']
        ecosystem = _ecosystem(package.get('ecosystem'))
        item = dict(id=_normalize_advisory_id(identifier), aliases=sorted({_normalize_advisory_id(alias) for alias in aliases}), ecosystem=ecosystem, package=package.get('name'), versions=affected_.get('versions', []), affected_ranges=affected_.get('ranges', []), record_version=2)
        for field in ('withdrawn', 'modified', 'published', 'schema_version', 'summary'):
            if field in raw:
                item[field] = raw[field]
        item['provenance'] = {'format': 'OSV', 'record_id': identifier,
                              'record_sha256': hashlib.sha256(json.dumps(raw, sort_keys=True, allow_nan=False).encode()).hexdigest()}
        result.append(item)
    return result


class _InventoryLimit(ValueError):
    pass


def _limit(value, lower, upper, name):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f'{name} must be an integer from {lower} through {upper}')


def _package(ecosystem, name):
    if not isinstance(name, str) or not (_NAME if ecosystem == 'pypi' else _NPM).fullmatch(name):
        raise ValueError('Invalid dependency package name')
    return re.sub(r'[-_.]+', '-', name).lower() if ecosystem == 'pypi' else name


def _ecosystem(value):
    if not isinstance(value, str) or value.lower() not in {'pypi', 'npm'}:
        raise ValueError('Supported ecosystems are pypi and npm')
    return value.lower()


def _exact(version):
    return isinstance(version, str) and len(version) <= 120 and bool(_VERSION.fullmatch(version))


def _safe_path(root: Path, target, *, private=False):
    if not isinstance(target, (str, Path)) or not str(target) or len(str(target)) > 4096:
        raise ValueError('Choose a bounded workspace path')
    lexical = Path(os.path.abspath(root / target))
    if not lexical.is_relative_to(root):
        raise ValueError('Path escapes workspace')
    current = root
    for part in lexical.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError('Symbolic links are not supported for dependency evidence')
        if not private and (part in IGNORED or part.startswith('.env') or part.endswith(('.pem', '.key'))):
            raise ValueError('Private state and credential files are excluded')
    if not lexical.resolve().is_relative_to(root):
        raise ValueError('Path escapes workspace')
    return lexical


def _read(root, path):
    checked = _safe_path(root, path, private=True)
    descriptor = os.open(checked, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_BYTES:
            raise ValueError('Dependency document exceeds the regular-file size limit')
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError('Dependency document exceeds size limit')
    return raw


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON keys')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, RecursionError, ValueError):
        raise ValueError('Invalid bounded dependency JSON') from None


class DependencyInventory:
    def __init__(self, project_root):
        self.root = Workspace(project_root).root

    def scan(self, target_path='.', max_manifests=50, max_components=2000):
        _limit(max_manifests, 1, 200, 'max_manifests')
        _limit(max_components, 1, 10000, 'max_components')
        target = _safe_path(self.root, target_path)
        if not target.exists():
            raise ValueError('Dependency target does not exist')
        components, relationships, unresolved = [], [], []
        coverage = {'status': 'complete', 'manifests_scanned': 0, 'parse_errors': 0, 'skipped': [], 'limits': {'max_manifests': max_manifests, 'max_components': max_components, 'max_document_bytes': _MAX_BYTES, 'max_files_considered': 10000}}
        considered = 0
        total_bytes = 0
        def candidates():
            if target.is_file():
                yield target
                return
            directories_seen = 0
            for directory, dirs, names in os.walk(target, followlinks=False):
                directories_seen += 1
                if directories_seen > 10000:
                    coverage['skipped'].append('Directory enumeration budget reached')
                    break
                dirs[:] = sorted(item for item in dirs if item not in IGNORED and not item.startswith('.env') and not (Path(directory) / item).is_symlink())
                for name in sorted(names):
                    yield Path(directory) / name
        def add(name, version, ecosystem, source, digest, location, direct=True, scope='runtime', resolution='declared_exact', conditional=False):
            name = _package(ecosystem, name)
            if len(components) >= max_components:
                raise _InventoryLimit('Component budget reached')
            identifier = hashlib.sha256(f'{source}\0{location}\0{name}\0{version}'.encode()).hexdigest()[:24]
            item = {'id': identifier, 'name': name, 'version': version, 'ecosystem': ecosystem, 'direct': direct, 'scope': scope, 'resolution': resolution, 'conditional': conditional, 'provenance': {'source': source, 'source_sha256': digest, 'location': location}}
            components.append(item)
            if version is None or conditional:
                unresolved.append({'component_id': identifier, 'reason': 'Environment marker not evaluated' if conditional else 'Dependency version not exactly resolved'})
            return identifier
        for path in candidates():
            considered += 1
            if considered > 10000:
                coverage['skipped'].append('File enumeration budget reached')
                break
            supported = path.name in _MANIFESTS or (path.name.startswith('requirements') and path.suffix == '.txt')
            if path.name in _UNSUPPORTED:
                coverage['skipped'].append(f'{path.relative_to(self.root).as_posix()}: unsupported lock format')
            if not supported:
                continue
            if coverage['manifests_scanned'] >= max_manifests:
                coverage['skipped'].append('Manifest budget reached')
                break
            source = path.relative_to(self.root).as_posix()
            try:
                _safe_path(self.root, path)
                raw = _read(self.root, path)
                total_bytes += len(raw)
                if total_bytes > 16_000_000:
                    coverage['skipped'].append('Combined manifest byte budget reached')
                    break
                digest = hashlib.sha256(raw).hexdigest()
                coverage['manifests_scanned'] += 1
                if path.name in {'package-lock.json', 'npm-shrinkwrap.json'}:
                    self._npm(_json(raw), source, digest, add, relationships, unresolved)
                else:
                    text = raw.decode('utf-8')
                    if path.name == 'pyproject.toml':
                        project = tomllib.loads(text).get('project', {})
                        entries = [('runtime', project.get('dependencies', []))]
                        entries += [(f'optional:{key}', value) for key, value in project.get('optional-dependencies', {}).items()]
                    else:
                        entries = [('runtime', text.splitlines())]
                    for scope, requirements in entries:
                        if not isinstance(requirements, list):
                            raise ValueError('Invalid dependency declarations')
                        for index, requirement in enumerate(requirements):
                            self._requirement(requirement, source, digest, f'{scope}:{index}', scope, add, unresolved)
            except _InventoryLimit as exc:
                coverage['skipped'].append(str(exc))
                break
            except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
                coverage['parse_errors'] += 1
                coverage['skipped'].append(f'{source}: manifest unavailable or not completely parsed')
        if not coverage['manifests_scanned']:
            coverage['skipped'].append('No supported dependency manifests were inspected')
        coverage['bytes_scanned'] = min(total_bytes, 16_000_000)
        coverage['files_considered'] = min(considered, 10000)
        coverage['skipped'] = coverage['skipped'][:100]
        coverage['status'] = 'partial' if unresolved or coverage['skipped'] else 'complete'
        return {'format': 'local-coder-dependency-inventory-v1', 'sbom_standard': None, 'components': components, 'relationships': relationships, 'unresolved': unresolved, 'coverage': coverage, 'verification': 'unverified', 'limitations': ['No package resolution or code execution', 'Environment markers and version ranges are not evaluated', 'Inventory does not establish installed versions or security status']}

    @staticmethod
    def _requirement(requirement, source, digest, location, scope, add, unresolved):
        if not isinstance(requirement, str) or len(requirement) > 10000:
            raise ValueError('Invalid requirement declaration')
        line = requirement.strip()
        if not line or line.startswith('#'):
            return
        if line.startswith('-'):
            unresolved.append({'source': source, 'reason': 'Requirement option or include not followed'})
            return
        match = re.match(r'([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]\r\n]*\])?(.*)', line)
        if not match:
            unresolved.append({'source': source, 'reason': 'Unsupported requirement declaration'})
            return
        name, tail = match.groups()
        body = tail.split(';', 1)[0].split(' #', 1)[0].strip()
        pinned = re.fullmatch(r'==\s*([^\s]+)(?:\s+--hash=\S+)*', body)
        version = pinned.group(1) if pinned and _exact(pinned.group(1)) else None
        add(name, version, 'pypi', source, digest, location, scope=scope, conditional=';' in tail, resolution='declared_exact' if version else 'unresolved')

    @staticmethod
    def _npm(document, source, digest, add, relationships, unresolved):
        if not isinstance(document, dict):
            raise ValueError('Invalid npm lock')
        version = document.get('lockfileVersion', 1)
        if version in (2, 3) and isinstance(document.get('packages'), dict):
            packages = document['packages']
            root = packages.get('', {})
            direct_names = set(root.get('dependencies', {})) | set(root.get('devDependencies', {})) | set(root.get('optionalDependencies', {}))
            identifiers = {}
            for location, item in packages.items():
                if not location:
                    continue
                if not isinstance(location, str) or not isinstance(item, dict) or '/..' in location or not location.startswith('node_modules/'):
                    unresolved.append({'source': source, 'reason': 'Unsupported linked or workspace npm package'})
                    continue
                name = location.rsplit('node_modules/', 1)[1]
                exact = item.get('version') if _exact(item.get('version')) else None
                identifiers[location] = add(name, exact, 'npm', source, digest, location, direct=location == f'node_modules/{name}' and name in direct_names, scope='development' if item.get('dev') else 'runtime', resolution='locked_exact' if exact else 'unresolved')
            for location, item in packages.items():
                if not identifiers.get(location):
                    continue
                for name in item.get('dependencies', {}):
                    if len(relationships) >= 20000:
                        raise _InventoryLimit('Dependency relationship budget reached')
                    _package('npm', name)
                    prefix = location
                    while True:
                        candidate = f'{prefix}/node_modules/{name}' if prefix else f'node_modules/{name}'
                        if identifiers.get(candidate):
                            relationships.append({'from': identifiers[location], 'to': identifiers[candidate], 'kind': 'depends_on'})
                            break
                        if not prefix:
                            unresolved.append({'component_id': identifiers[location], 'reason': 'Npm dependency edge not present in lock inventory'})
                            break
                        prefix = prefix.rsplit('/node_modules/', 1)[0] if '/node_modules/' in prefix else ''
        elif version == 1 and isinstance(document.get('dependencies', {}), dict):
            pending = [(name, item, '', None) for name, item in document.get('dependencies', {}).items()]
            visited = 0
            while pending:
                name, item, parent_location, parent_id = pending.pop()
                visited += 1
                if visited > 20000:
                    unresolved.append({'source': source, 'reason': 'Legacy lock traversal budget reached'})
                    break
                location = f'{parent_location}/node_modules/{name}' if parent_location else f'node_modules/{name}'
                exact = item.get('version') if _exact(item.get('version')) else None
                identifier = add(name, exact, 'npm', source, digest, location, direct=not parent_location, resolution='locked_exact' if exact else 'unresolved')
                if identifier and parent_id:
                    if len(relationships) >= 20000:
                        raise _InventoryLimit('Dependency relationship budget reached')
                    relationships.append({'from': parent_id, 'to': identifier, 'kind': 'depends_on'})
                pending.extend((child, details, location, identifier) for child, details in item.get('dependencies', {}).items())
        else:
            raise ValueError('Unsupported npm lock version')


class LocalAdvisoryStore:
    """Human-facing import API; deliberately absent from model tool actions."""
    def __init__(self, project_root):
        self.root = Workspace(project_root).root

    @staticmethod
    def _validate(payload):
        try:
            encoded = json.dumps(payload, allow_nan=False).encode()
        except (ValueError, TypeError, RecursionError):
            raise ValueError('Invalid advisory JSON') from None
        if len(encoded) > _MAX_BYTES or not isinstance(payload, dict) or set(payload) != {'version', 'advisories'} or type(payload['version']) is not int or payload['version'] not in {1, 2} or not isinstance(payload['advisories'], list) or len(payload['advisories']) > _MAX_ADVISORIES:
            raise ValueError('Unsupported advisory document or size limit')
        incoming = []
        for raw in payload['advisories']:
            incoming.extend(_osv_records(raw) if payload['version'] == 2 and isinstance(raw, dict) and 'affected' in raw else [raw])
        if len(incoming) > _MAX_ADVISORIES:
            raise ValueError('Advisory record budget reached')
        result = []
        for raw in incoming:
            if not isinstance(raw, dict) or set(raw) - {'id', 'ecosystem', 'package', 'versions', 'affected_ranges', 'summary', 'severity', 'reference', 'record_version', 'aliases', 'withdrawn', 'modified', 'published', 'schema_version', 'provenance'} or not {'id', 'ecosystem', 'package'} <= set(raw):
                raise ValueError('Unknown or missing advisory fields')
            if not isinstance(raw['id'], str) or not (re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', raw['id']) if raw.get('record_version') == 2 or payload['version'] == 2 else _ID.fullmatch(raw['id'])):
                raise ValueError('Invalid CVE or GHSA identifier')
            ecosystem = _ecosystem(raw['ecosystem'])
            item = {'id': raw['id'], 'ecosystem': ecosystem, 'package': _package(ecosystem, raw['package'])}
            modern = raw.get('record_version') == 2 or payload['version'] == 2
            if modern:
                item['record_version'] = 2
                item['id'] = _normalize_advisory_id(item['id'])
                for field in ('aliases', 'withdrawn', 'modified', 'published', 'schema_version', 'provenance'):
                    if field in raw:
                        value = raw[field]
                        if field == 'aliases':
                            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', v) for v in value):
                                raise ValueError('Invalid advisory aliases')
                            value = sorted({_normalize_advisory_id(alias) for alias in value})
                        elif field == 'provenance':
                            if not isinstance(value, dict) or len(value) > 10 or any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 100 or len(v) > 2048 for k, v in value.items()):
                                raise ValueError('Invalid advisory provenance')
                        elif not isinstance(value, str) or len(value) > 100 or any(ord(c) < 32 for c in value):
                            raise ValueError('Invalid advisory metadata')
                        if field in {'withdrawn', 'modified', 'published'}:
                            try:
                                timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                                if timestamp.tzinfo is None:
                                    raise ValueError('Missing timezone')
                            except ValueError:
                                raise ValueError('Invalid advisory timestamp') from None
                        item[field] = value
            versions = raw.get('versions', [])
            ranges = raw.get('affected_ranges', [])
            if not isinstance(versions, list) or len(versions) > 500 or any(not _exact(value) for value in versions):
                raise ValueError('Advisory versions must be explicit exact versions')
            if not isinstance(ranges, list) or len(ranges) > 100 or any(not (modern and isinstance(value, dict) and len(json.dumps(value)) <= 30000) and (not isinstance(value, str) or not 0 < len(value) <= 200 or not re.fullmatch(r'[A-Za-z0-9.*<>=!~^+|, _-]+', value)) for value in ranges):
                raise ValueError('Invalid advisory ranges')
            if not versions and not ranges and not item.get('withdrawn'):
                raise ValueError('Advisories require exact versions or unresolved ranges')
            item.update(versions=list(dict.fromkeys(versions)), affected_ranges=ranges)
            for field, maximum in [('summary', 2000), ('severity', 40), ('reference', 2048)]:
                if field not in raw:
                    continue
                value = raw[field]
                if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 for char in value):
                    raise ValueError('Invalid advisory text')
                if field == 'reference':
                    try:
                        url = urlsplit(value)
                        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                            raise ValueError('References must be HTTPS URLs without credentials or query data')
                    except ValueError:
                        raise ValueError('Invalid public advisory reference') from None
                item[field] = _clean(value)
            result.append(item)
        return result

    def _load(self):
        target = _safe_path(self.root, _CACHE, private=True)
        if not target.exists():
            return [], 'cache_absent', None
        document = _json(_read(self.root, target))
        if not isinstance(document, dict) or set(document) != {'version', 'advisories', 'imported_at', 'source_label'}:
            raise ValueError('Invalid advisory cache')
        if not isinstance(document['imported_at'], str) or len(document['imported_at']) > 100 or not isinstance(document['source_label'], str) or len(document['source_label']) > 200:
            raise ValueError('Invalid advisory provenance')
        records = self._validate({'version': document['version'], 'advisories': document['advisories']})
        return records, 'local_import', {'imported_at': document['imported_at'], 'source_label': _clean(document['source_label'])}

    def import_records(self, payload, source_label='operator-import'):
        # Withdrawn OSV records may omit affected packages. Apply that tombstone
        # to cached package records instead of leaving stale positive evidence.
        if isinstance(payload, dict) and payload.get('version') == 2 and isinstance(payload.get('advisories'), list):
            incoming = []
            for raw in payload['advisories']:
                if isinstance(raw, dict) and raw.get('withdrawn') and not raw.get('affected') and 'package' not in raw:
                    prior, _, _ = self._load()
                    identifier = _normalize_advisory_id(raw.get('id', '')) if isinstance(raw.get('id'), str) else ''
                    cached = [item for item in prior if item['id'] == identifier or identifier in item.get('aliases', [])]
                    if not cached:
                        raise ValueError('Withdrawn record has no package evidence to update')
                    incoming.extend({**item, 'record_version': 2, 'withdrawn': raw['withdrawn']} for item in cached)
                else:
                    incoming.append(raw)
            payload = {**payload, 'advisories': incoming}
        records = self._validate(payload)
        if not isinstance(source_label, str) or not 0 < len(source_label) <= 200 or any(ord(char) < 32 for char in source_label):
            raise ValueError('Invalid advisory source label')
        _safe_path(self.root, _CACHE, private=True)
        target = state_path(self.root, _CACHE)
        target.parent.chmod(0o700)
        lock = _safe_path(self.root, '.local-coder/security/advisories.lock', private=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError('Advisory lock must be a regular file')
        with os.fdopen(fd, 'a+b') as handle, exclusive_lock(handle):
            prior, _, _ = self._load()
            merged = {(item['id'], item['ecosystem'], item['package']): item for item in prior}
            merged.update({(item['id'], item['ecosystem'], item['package']): item for item in records})
            if len(merged) > _MAX_ADVISORIES:
                raise ValueError('Advisory cache record budget reached')
            document = {'version': 1, 'advisories': list(merged.values()), 'imported_at': datetime.now(UTC).isoformat(), 'source_label': _clean(source_label)}
            encoded = json.dumps(document, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > _MAX_BYTES:
                raise ValueError('Advisory cache byte budget reached')
            fd, temporary = tempfile.mkstemp(prefix='.advisories-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                _safe_path(self.root, _CACHE, private=True)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return {'imported': len(records), 'total': len(merged), 'verification': 'unverified', 'cache_status': 'local_import'}

    def query(self, ecosystem=None, package=None, version=None, advisory_id=None):
        if ecosystem is not None:
            ecosystem = _ecosystem(ecosystem)
        if package is not None:
            if ecosystem is None:
                raise ValueError('Package queries require an ecosystem')
            package = _package(ecosystem, package)
        if version is not None and not _exact(version):
            raise ValueError('Query version must be exact')
        if advisory_id is not None and (not isinstance(advisory_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', advisory_id)):
            raise ValueError('Invalid advisory identifier')
        if advisory_id is not None:
            advisory_id = _normalize_advisory_id(advisory_id)
        records, cache_status, provenance = self._load()
        return self._query_records(records, cache_status, provenance, ecosystem, package, version, advisory_id)

    @staticmethod
    def _query_records(records, cache_status, provenance, ecosystem=None, package=None, version=None, advisory_id=None):
        relevant = [item for item in records if (ecosystem is None or item['ecosystem'] == ecosystem) and (package is None or item['package'] == package) and (advisory_id is None or item['id'] == advisory_id or advisory_id in item.get('aliases', []))]
        matches, unresolved = [], []
        for item in relevant:
            matched, kind, reasons = advisory_match(item, version) if version is not None else (not item.get('withdrawn'), 'imported_record', [])
            unresolved.extend({'id': item['id'], 'reason': reason} for reason in reasons)
            if matched:
                matches.append({**item, 'match_type': kind, 'verification': 'unverified'})
        return {'matches': matches, 'unresolved': unresolved, 'cache_status': cache_status, 'status': 'matches' if matches else 'package_not_in_cache' if not relevant else 'unresolved' if unresolved else 'no_known_match', 'verification': 'unverified', 'no_match_is_not_safety_evidence': True, 'provenance': provenance}

"""Offline dependency inventory and operator-imported advisory evidence.

No package manager, resolver, HTTP client, or project code runs here. This is an
inventory format, not a standards-certified SBOM. Exact imported versions are
matched literally; ranges and environment markers remain unresolved.
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
        if len(encoded) > _MAX_BYTES or not isinstance(payload, dict) or set(payload) != {'version', 'advisories'} or type(payload['version']) is not int or payload['version'] != 1 or not isinstance(payload['advisories'], list) or len(payload['advisories']) > _MAX_ADVISORIES:
            raise ValueError('Unsupported advisory document or size limit')
        result = []
        for raw in payload['advisories']:
            if not isinstance(raw, dict) or set(raw) - {'id', 'ecosystem', 'package', 'versions', 'affected_ranges', 'summary', 'severity', 'reference'} or not {'id', 'ecosystem', 'package'} <= set(raw):
                raise ValueError('Unknown or missing advisory fields')
            if not isinstance(raw['id'], str) or not _ID.fullmatch(raw['id']):
                raise ValueError('Invalid CVE or GHSA identifier')
            ecosystem = _ecosystem(raw['ecosystem'])
            item = {'id': raw['id'], 'ecosystem': ecosystem, 'package': _package(ecosystem, raw['package'])}
            versions = raw.get('versions', [])
            ranges = raw.get('affected_ranges', [])
            if not isinstance(versions, list) or len(versions) > 500 or any(not _exact(value) for value in versions):
                raise ValueError('Advisory versions must be explicit exact versions')
            if not isinstance(ranges, list) or len(ranges) > 100 or any(not isinstance(value, str) or not 0 < len(value) <= 200 or not re.fullmatch(r'[A-Za-z0-9.*<>=!~^+|, _-]+', value) for value in ranges):
                raise ValueError('Invalid advisory ranges')
            if not versions and not ranges:
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
        if advisory_id is not None and (not isinstance(advisory_id, str) or not _ID.fullmatch(advisory_id)):
            raise ValueError('Invalid advisory identifier')
        records, cache_status, provenance = self._load()
        return self._query_records(records, cache_status, provenance, ecosystem, package, version, advisory_id)

    @staticmethod
    def _query_records(records, cache_status, provenance, ecosystem=None, package=None, version=None, advisory_id=None):
        relevant = [item for item in records if (ecosystem is None or item['ecosystem'] == ecosystem) and (package is None or item['package'] == package) and (advisory_id is None or item['id'] == advisory_id)]
        matches, unresolved = [], []
        for item in relevant:
            if item['affected_ranges']:
                unresolved.append({'id': item['id'], 'reason': 'Imported affected ranges are not evaluated'})
            if version is None or version in item['versions']:
                matches.append({**item, 'match_type': 'exact_imported_version' if version else 'imported_record', 'verification': 'unverified'})
        return {'matches': matches, 'unresolved': unresolved, 'cache_status': cache_status, 'status': 'matches' if matches else 'package_not_in_cache' if not relevant else 'no_exact_version_match', 'verification': 'unverified', 'no_match_is_not_safety_evidence': True, 'provenance': provenance}

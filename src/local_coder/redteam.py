"""Durable, workspace-local engagement scope and evidence; no model-owned scope edits."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from local_coder.memory.context_store import _clean, _PRIVATE_NAMES, _PRIVATE_SUFFIXES
from local_coder.file_lock import exclusive_lock
from local_coder.workspace import Workspace

PHASES = ('scope', 'reconnaissance', 'analysis', 'validation', 'report')
_SOURCE_DIRFD_SUPPORTED = os.open in os.supports_dir_fd and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW')


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {'', '/'}
            or any(c.isspace() or c == '\\' for c in value) or '*' in value):
        raise ValueError('Use an exact HTTP(S) origin without credentials, path, query or wildcards')
    host = parsed.hostname.encode('idna').decode('ascii').lower()
    if host.endswith('.'):
        raise ValueError('Trailing-dot hostnames are not supported')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    host = f'[{host}]' if ':' in host else host
    return f'{parsed.scheme}://{host}:{port}'


def url_asset(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid URL characters')
    parsed = urlsplit(value)
    base = origin(f'{parsed.scheme}://{parsed.netloc}')
    if parsed.query or parsed.fragment:
        raise ValueError('HTTP observations do not accept query payloads or fragments')
    path = parsed.path or '/'
    for _ in range(4):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if ('%' in path or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or '..' in path.split('/') or path.startswith('//')):
        raise ValueError('Ambiguous or traversing URL path')
    return base, path


def addresses(host: str, port: int) -> list[str]:
    resolved = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not resolved:
        raise ValueError('Host has no addresses')
    for value in resolved:
        address = ipaddress.ip_address(value)
        if address.is_link_local or address.is_multicast or address.is_unspecified or (address.is_reserved and not address.is_loopback):
            raise ValueError('Link-local, reserved, multicast and unspecified destinations are not supported')
    return resolved


class EngagementScope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=4000)
    paths: list[str] = Field(default_factory=lambda: ['.'], min_length=1, max_length=20)
    excluded_paths: list[str] = Field(default_factory=list, max_length=40)
    origins: list[str] = Field(default_factory=list, max_length=20)
    excluded_urls: list[str] = Field(default_factory=list, max_length=40)
    authorization: str = Field(default='', max_length=2000)
    active_http: bool = False
    max_requests: int = Field(default=20, ge=1, le=50)

    @model_validator(mode='after')
    def validate_scope(self):
        self.origins = sorted({origin(item) for item in self.origins})
        for value in self.excluded_urls:
            url_asset(value)
        if self.active_http and (not self.origins or not self.authorization.strip()):
            raise ValueError('Active HTTP checks require exact allowed origins and an authorization reference')
        return self


class EngagementStore:
    def __init__(self, project_root: str):
        self.workspace = Workspace(project_root)

    def _path(self, identity: str) -> Path:
        if not re.fullmatch(r'eng-[a-f0-9]{12}', identity):
            raise ValueError('Invalid engagement ID')
        path = self.workspace.resolve(f'.local-coder/engagements/{identity}.json')
        expected = self.workspace.root / '.local-coder/engagements'
        if path.parent != expected:
            raise ValueError('Engagement state cannot be a symbolic link')
        return path

    def create(self, scope: EngagementScope) -> dict:
        for field in ('paths', 'excluded_paths'):
            setattr(scope, field, [self.workspace.resolve(value).relative_to(self.workspace.root).as_posix() for value in getattr(scope, field)])
        for path in scope.paths:
            if not self.workspace.resolve(path).exists():
                raise ValueError('In-scope paths must exist')
        pins = {}
        if scope.active_http:
            for value in scope.origins:
                parsed = urlsplit(value)
                pins[value] = addresses(parsed.hostname, parsed.port)
        record = {'id': 'eng-' + uuid4().hex[:12], 'scope': scope.model_dump(), 'pins': pins,
                  'created_at': self.now(), 'phase': 'scope', 'steps': [], 'requests_used': 0,
                  'events': [], 'findings': [], 'completed_phases': []}
        path = self._path(record['id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write(path, record)
        return record

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def _write(self, path, record):
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if len(payload.encode()) > 5_000_000:
            raise ValueError('Engagement evidence budget reached; export this report and start a new engagement')
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.engagement-')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self, identity: str) -> dict:
        path = self._path(identity)
        if not path.is_file() or path.stat().st_size > 5_000_000:
            raise ValueError('Engagement not found or exceeds size limit')
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('id') != identity:
            raise ValueError('Engagement identity mismatch')
        EngagementScope.model_validate(record['scope'])
        previous = ''
        for event in record['events']:
            content = {key: value for key, value in event.items() if key != 'sha256'}
            digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            if event['previous_sha256'] != previous or event['sha256'] != digest:
                raise ValueError('Evidence journal integrity check failed')
            previous = digest
        return record

    def update(self, identity, mutate):
        path = self._path(identity)
        lock = self.workspace.resolve(str(path.with_suffix('.lock')))
        with lock.open('a+', encoding='utf-8') as stream, exclusive_lock(stream):
            record = self.load(identity)
            result = mutate(record)
            self._write(path, record)
            return result

    def list(self):
        folder = self.workspace.resolve('.local-coder/engagements')
        if not folder.exists():
            return []
        records = []
        for path in sorted(folder.glob('eng-*.json'))[:200]:
            record = self.load(path.stem)
            records.append({'id': record['id'], 'name': record['scope']['name'], 'phase': record['phase'], 'created_at': record['created_at']})
        return records

    @staticmethod
    def append(record, kind, data):
        if len(record['events']) >= 500:
            raise ValueError('Engagement evidence event budget reached')
        event = {'id': 'ev-' + uuid4().hex[:12], 'timestamp': EngagementStore.now(), 'kind': kind,
                 'data': data, 'previous_sha256': record['events'][-1]['sha256'] if record['events'] else ''}
        event['sha256'] = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        record['events'].append(event)
        return event['id']

    def evidence(self, identity, kind, data):
        return self.update(identity, lambda record: self.append(record, kind, data))

    def resolve_source(self, record, value):
        path = self.workspace.resolve(value)
        relative = path.relative_to(self.workspace.root)
        scope = record['scope']
        def under(parent):
            return path.is_relative_to(self.workspace.resolve(parent))
        if (not any(under(parent) for parent in scope['paths']) or any(under(parent) for parent in scope['excluded_paths'])
                or any(part in {'.local-coder', '.git'} for part in relative.parts)
                or path.name.lower().startswith(('.env', 'secrets.', 'credentials.'))
                or path.name.lower() in _PRIVATE_NAMES or path.suffix.lower() in _PRIVATE_SUFFIXES):
            raise ValueError('Asset is outside engagement scope or contains credential/state data')
        return path

    @contextmanager
    def _source_descriptor(self, path):
        """Pin every directory component on platforms with nofollow openat.

        Other platforms use conservative pre/post identity checks. Those checks
        reject observed ancestor changes but do not provide an openat guarantee.
        """
        descriptors, directories = [], []
        def validate_directories():
            for directory, expected in directories:
                current = directory.lstat()
                if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != expected:
                    raise ValueError('Source ancestor changed while reading; repeat the observation')
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            if _SOURCE_DIRFD_SUPPORTED:
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                current_path = Path(path.anchor)
                current_fd = os.open(current_path, directory_flags)
                descriptors.append(current_fd)
                info = os.fstat(current_fd)
                directories.append((current_path, (info.st_dev, info.st_ino)))
                for component in path.parts[1:-1]:
                    current_fd = os.open(component, directory_flags, dir_fd=current_fd)
                    descriptors.append(current_fd)
                    info = os.fstat(current_fd)
                    current_path = current_path / component
                    directories.append((current_path, (info.st_dev, info.st_ino)))
                descriptor = os.open(path.name, flags, dir_fd=current_fd)
            else:
                for directory in reversed(path.parents):
                    info = directory.lstat()
                    if not stat.S_ISDIR(info.st_mode):
                        raise ValueError('Source ancestors must be unlinked directories')
                    directories.append((directory, (info.st_dev, info.st_ino)))
                descriptor = os.open(path, flags)
            descriptors.append(descriptor)
            validate_directories()
            opened, located = os.fstat(descriptor), path.lstat()
            if not stat.S_ISREG(located.st_mode) or (opened.st_dev, opened.st_ino) != (located.st_dev, located.st_ino):
                raise ValueError('Source changed while opening; repeat the observation')
            yield descriptor
            validate_directories()
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def source_bytes(self, record, value, *, max_bytes=64000):
        """Read a scoped regular source without following links or blocking on FIFOs."""
        candidate = self.workspace.root / Path(value)
        if any(part.is_symlink() for part in (candidate, *candidate.parents) if part.is_relative_to(self.workspace.root)):
            raise ValueError('Source paths must not use symbolic links')
        path = self.resolve_source(record, value)
        if _clean(path.relative_to(self.workspace.root).as_posix()) != path.relative_to(self.workspace.root).as_posix():
            raise ValueError('Source path contains credential-like data')
        with self._source_descriptor(path) as descriptor:
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
                    raise ValueError('Choose an unlinked regular source file within the size limit')
                raw = stream.read(max_bytes + 1)
                after = os.fstat(stream.fileno())
                current = path.lstat()
                identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
                if len(raw) > max_bytes or len(raw) != before.st_size or identity(before) != identity(after) or identity(after) != identity(current):
                    raise ValueError('Source changed while reading; repeat the observation')
        return path, raw, hashlib.sha256(raw).hexdigest()

    def source_hash(self, record, value):
        return self.source_bytes(record, value)[2]

    def _evidence_assets(self, record, event):
        data, kind = event.get('data', {}), event.get('kind')
        result = {}
        if kind == 'source_read' and isinstance(data.get('path'), str):
            result[data['path']] = data.get('sha256')
        elif kind in {'static_scan', 'scanner_import'}:
            hashes = data.get('source_hashes', {})
            if isinstance(hashes, dict):
                result.update({key: value for key, value in list(hashes.items())[:1000] if isinstance(key, str)})
            assets = data.get('assets', data.get('files', []))
            if isinstance(assets, list):
                for asset in assets[:1000]:
                    if isinstance(asset, str):
                        result.setdefault(asset, None)
        elif kind == 'http_observation' and isinstance(data.get('url'), str):
            result[data['url']] = None
        elif kind == 'proxy_history':
            for observation in data.get('observations', [])[:100]:
                if isinstance(observation, dict) and isinstance(observation.get('url'), str):
                    result[observation['url']] = None
        canonical = {}
        for asset, digest in result.items():
            try:
                if '://' in asset:
                    self.check_url(record, asset)
                    base, path = url_asset(asset)
                    key = base + path
                else:
                    key = self.resolve_source(record, asset).relative_to(self.workspace.root).as_posix()
                canonical[key] = digest if isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest) else None
            except (ValueError, OSError):
                continue
        return canonical

    def _asset_freshness(self, record, asset, digest, cache):
        if '://' in asset or not digest:
            return 'unknown'
        if asset not in cache:
            if len(cache) >= 1000 or sum(item[1] for item in cache.values()) >= 20_000_000:
                return 'unknown'
            try:
                _, raw, current = self.source_bytes(record, asset)
                cache[asset] = (current, len(raw))
            except (FileNotFoundError, ValueError, OSError):
                cache[asset] = (None, 0)
        return 'current' if cache[asset][0] == digest else 'stale'

    def _empty_scan_freshness(self, record, data):
        """Recheck empty membership without reading code or unbounded traversal."""
        from local_coder.tools.scan_scope import IGNORED
        targets = data.get('scan_targets')
        if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
            return 'unknown'
        pending, seen, entries = [], set(), 0
        try:
            for value in targets:
                path = self.resolve_source(record, value)
                if not path.is_dir() or path.is_symlink():
                    return 'stale'
                pending.append(path)
            while pending:
                path = pending.pop()
                if path in seen:
                    continue
                seen.add(path)
                if len(seen) > 256:
                    return 'unknown'
                with os.scandir(path) as children:
                    for child in children:
                        entries += 1
                        if entries > 2000:
                            return 'unknown'
                        try:
                            scoped = self.resolve_source(record, child.path)
                        except ValueError:
                            continue
                        if child.is_symlink():
                            return 'unknown'
                        if child.is_dir(follow_symlinks=False):
                            if child.name not in IGNORED:
                                pending.append(scoped)
                        else:
                            return 'stale'
            return 'current'
        except (OSError, ValueError, TypeError):
            return 'unknown'

    def investigation_snapshot(self, identity):
        record = self.load(identity)
        cache, summaries, coverage, gaps = {}, [], [], []
        kinds = {'source_read', 'static_scan', 'scanner_import', 'http_observation', 'proxy_history'}
        events = [event for event in record['events'] if event['kind'] in kinds]
        lookup = {event['id']: event for event in events}
        for event in events[-20:]:
            data = event['data']
            assets = self._evidence_assets(record, event)
            statuses = [self._asset_freshness(record, asset, digest, cache) for asset, digest in assets.items()]
            status = 'stale' if 'stale' in statuses else 'current' if statuses and all(item == 'current' for item in statuses) else 'unknown'
            details = data.get('coverage', {})
            if event['kind'] == 'static_scan' and not assets and data.get('files') == [] and details.get('status') == 'complete' and data.get('source_hashes') == {}:
                status = self._empty_scan_freshness(record, data)
            if details.get('changed_sources'):
                status = 'stale'
            summary = {'id': event['id'], 'kind': event['kind'], 'freshness': status,
                       'asset_count': len(assets), 'assets': list(assets)[:20]}
            if event['kind'] == 'source_read':
                summary.update(path=next(iter(assets), None), sha256=next(iter(assets.values()), None))
            if event['kind'] in {'static_scan', 'scanner_import'}:
                hashes = data.get('source_hashes', {})
                summary.update(content_digest=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
                               finding_count=len(data.get('findings', [])) if event['kind'] == 'static_scan' else data.get('candidate_count', 0),
                               coverage_status=details.get('status', 'unknown'))
            if details:
                coverage.append({'evidence_id': event['id'], 'status': details.get('status', 'unknown'),
                                 'files_considered': details.get('files_considered'), 'bytes': details.get('bytes'),
                                 'skipped_count': len(details.get('skipped', [])),
                                 'changed_sources': details.get('changed_sources', [])[:20]})
            summaries.append(summary)
        findings = []
        for finding in record['findings'][-20:]:
            asset = finding['asset']
            try:
                if '://' in asset:
                    base, path = url_asset(asset)
                    asset = base + path
                else:
                    asset = self.resolve_source(record, asset).relative_to(self.workspace.root).as_posix()
            except (ValueError, OSError):
                pass
            statuses = []
            for evidence_id in finding['evidence_ids'][:20]:
                event = lookup.get(evidence_id)
                assets = self._evidence_assets(record, event) if event else {}
                statuses.append('stale' if event and asset in event['data'].get('coverage', {}).get('changed_sources', []) else self._asset_freshness(record, asset, assets[asset], cache) if asset in assets else 'unknown')
            status = 'stale' if 'stale' in statuses else 'current' if statuses and all(item == 'current' for item in statuses) else 'unknown'
            findings.append({'id': finding['id'], 'asset': finding['asset'], 'title': finding['title'][:200],
                             'evidence_ids': finding['evidence_ids'][:20], 'source_status': status})
        if any(item['freshness'] == 'stale' for item in summaries):
            gaps.append('Source observations changed or disappeared; re-read or re-scan affected scoped files')
        if any(item['freshness'] == 'unknown' for item in summaries):
            gaps.append('Some observations lack verifiable current source hashes')
        if not any(item['kind'] in {'source_read', 'static_scan'} and item['freshness'] == 'current' for item in summaries):
            gaps.append('No current local source observation is available')
        if any(item['status'] != 'complete' for item in coverage):
            gaps.append('Static coverage is partial or unknown; narrow the scope or inspect skipped files')
        snapshot = _clean({'phase': record['phase'], 'scope': record['scope'], 'steps': record['steps'][:30],
                           'evidence': summaries, 'findings': findings, 'coverage': coverage,
                           'counts': {'evidence': len(events), 'findings': len(record['findings'])}, 'gaps': gaps})
        # Preserve absence as absence without exposing authorization references.
        snapshot['scope']['authorization'] = '[REDACTED]' if record['scope'].get('authorization') else ''
        return snapshot

    def check_url(self, record, value):
        base, path = url_asset(value)
        if base not in record['scope']['origins']:
            raise ValueError('URL origin is outside engagement scope')
        for excluded in record['scope']['excluded_urls']:
            excluded_base, excluded_path = url_asset(excluded)
            if base == excluded_base and (path == excluded_path or path.startswith(excluded_path.rstrip('/') + '/')):
                raise ValueError('URL is excluded from engagement scope')
        return base

    def reserve_request(self, identity, value, method):
        def reserve(record):
            if not record['scope']['active_http']:
                raise ValueError('Active HTTP checks are disabled for this engagement')
            self.check_url(record, value)
            if record['requests_used'] >= record['scope']['max_requests']:
                raise ValueError('Engagement HTTP request budget exhausted')
            record['requests_used'] += 1
            return self.append(record, 'http_attempt', {'url': value, 'method': method})
        return self.update(identity, reserve)

    def finding(self, identity, finding):
        def save(record):
            asset = finding['asset']
            if '://' in asset:
                self.check_url(record, asset)
            else:
                self.resolve_source(record, asset)
            known = {event['id']: event for event in record['events'] if event['kind'] in {'source_read', 'static_scan', 'http_observation', 'scanner_import', 'proxy_history'}}
            if not finding['evidence_ids'] or not set(finding['evidence_ids']) <= known.keys():
                raise ValueError('Findings require evidence IDs from this engagement')
            if '://' in asset:
                base, path = url_asset(asset)
                canonical = base + path
            else:
                canonical = self.resolve_source(record, asset).relative_to(self.workspace.root).as_posix()
            cache = {}
            for evidence_id in finding['evidence_ids']:
                event = known[evidence_id]
                assets = self._evidence_assets(record, event)
                if canonical not in assets:
                    raise ValueError('Finding evidence must describe the same scoped asset')
                if self._asset_freshness(record, canonical, assets[canonical], cache) == 'stale' or canonical in event['data'].get('coverage', {}).get('changed_sources', []):
                    raise ValueError('Finding source evidence is stale; re-read or re-scan the asset')
            identity_hash = hashlib.sha256((asset + '\n' + finding['title'].strip().casefold()).encode()).hexdigest()[:16]
            saved = {**finding, 'id': 'finding-' + identity_hash, 'verification': 'unverified'}
            existing = next((item for item in record['findings'] if item['id'] == saved['id']), None)
            if existing:
                retained = []
                for evidence_id in existing['evidence_ids']:
                    event = known.get(evidence_id)
                    assets = self._evidence_assets(record, event) if event else {}
                    if canonical in assets and self._asset_freshness(record, canonical, assets[canonical], cache) != 'stale' and canonical not in event['data'].get('coverage', {}).get('changed_sources', []):
                        retained.append(evidence_id)
                # Prior observations remain in the immutable journal. Current
                # finding links must not resurrect stale evidence on refresh.
                submitted = sorted(set(saved['evidence_ids']))
                saved['evidence_ids'] = submitted + sorted(set(retained) - set(submitted))[:20 - len(submitted)]
                existing.update(saved)
            else:
                record['findings'].append(saved)
            self.append(record, 'finding_recorded', {'finding': saved})
            return saved
        return self.update(identity, save)

    def report(self, identity):
        record = self.load(identity)
        scope = record['scope']
        snapshot = self.investigation_snapshot(identity)
        lines = [f"# {scope['name']}", '', scope['objective'], '', f"Phase: {record['phase']}",
                 f"Local scope: {', '.join(scope['paths'])}", f"Excluded: {', '.join(scope['excluded_paths']) or 'none'}",
                 f"HTTP scope: {', '.join(scope['origins']) or 'none'}", f"Excluded URLs: {', '.join(scope['excluded_urls']) or 'none'}", f"Active HTTP enabled: {scope['active_http']}", f"HTTP checks: {record['requests_used']}/{scope['max_requests']}",
                 '', '## Coverage and limitations',
                 'Findings are unverified hypotheses. HTTP observations and imported scanner output do not prove exploitation.',
                 'No credential attacks, persistence, lateral movement, destructive actions or arbitrary shell execution are provided by this mode.',
                 '', '## Workflow']
        lines += [f"- [{step['status']}] {step['title']}" for step in record['steps']]
        lines += ['', '## Unverified findings']
        for finding in record['findings']:
            lines += [f"### {finding['title']} ({finding.get('severity', 'info')})", f"Asset: {finding['asset']}" + (f":{finding['line']}" if finding.get('line') else ''),
                      finding.get('hypothesis', ''), f"Evidence: {', '.join(finding['evidence_ids'])}",
                      f"Attack path: {' → '.join(finding.get('attack_path', []))}",
                      f"Detection expected: {finding.get('detection_expected', '')}",
                      f"Detection outcome: {finding.get('detection_outcome', 'not_tested')}",
                      f"Remediation: {finding.get('remediation', '')}"]
        lines += ['', '## Source freshness and gaps']
        lines += [f"- {finding['id']}: {finding['source_status']}" for finding in snapshot['findings']]
        lines += [f"- {gap}" for gap in snapshot['gaps']]
        lines += ['', '## Evidence journal']
        lines += [f"- {event['id']} · {event['timestamp']} · {event['kind']} · {json.dumps(event['data'], ensure_ascii=False)}" for event in record['events']]
        return {'engagement': record, 'markdown': '\n'.join(lines)}

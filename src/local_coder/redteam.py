"""Durable, workspace-local engagement scope and evidence; no model-owned scope edits."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from local_coder.file_lock import exclusive_lock
from local_coder.workspace import Workspace

PHASES = ('scope', 'reconnaissance', 'analysis', 'validation', 'report')


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
        if not address.is_loopback:
            raise ValueError('Active HTTP observations are restricted to loopback addresses')
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
        if self.active_http:
            for value in self.origins:
                host = urlsplit(value).hostname or ""
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    loopback = host == "localhost"
                if not loopback:
                    raise ValueError('Active HTTP observations are restricted to localhost and loopback IPs')
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
                or path.name.startswith('.env') or path.suffix in {'.pem', '.key'}):
            raise ValueError('Asset is outside engagement scope or contains credential/state data')
        return path

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
            known = {event['id'] for event in record['events'] if event['kind'] in {'source_read', 'static_scan', 'http_observation', 'scanner_import', 'proxy_history'}}
            if not finding['evidence_ids'] or not set(finding['evidence_ids']) <= known:
                raise ValueError('Findings require evidence IDs from this engagement')
            identity_hash = hashlib.sha256((asset + '\n' + finding['title'].strip().casefold()).encode()).hexdigest()[:16]
            saved = {**finding, 'id': 'finding-' + identity_hash, 'verification': 'unverified'}
            existing = next((item for item in record['findings'] if item['id'] == saved['id']), None)
            if existing:
                saved['evidence_ids'] = sorted(set(existing['evidence_ids'] + saved['evidence_ids']))
                existing.update(saved)
            else:
                record['findings'].append(saved)
            self.append(record, 'finding_recorded', {'finding': saved})
            return saved
        return self.update(identity, save)

    def report(self, identity):
        record = self.load(identity)
        scope = record['scope']
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
        lines += ['', '## Evidence journal']
        lines += [f"- {event['id']} · {event['timestamp']} · {event['kind']} · {json.dumps(event['data'], ensure_ascii=False)}" for event in record['events']]
        return {'engagement': record, 'markdown': '\n'.join(lines)}

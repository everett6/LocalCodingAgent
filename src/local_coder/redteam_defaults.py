"""Private local-review preferences, parsed as data rather than shell input."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

from local_coder.redteam import EngagementScope, EngagementStore, origin, url_asset
from local_coder.workspace import Workspace


LOCAL_AUTHORIZATION = 'The operator requested a local source-code security review of this project.'
LOCAL_OBJECTIVE = 'Review the local project source for security weaknesses and report evidence-linked findings.'
_SCOPE_KEYS = set(EngagementScope.model_fields)
_META_KEYS = {'preset', 'last_engagement_id'}
_ENV_KEYS = {'REDTEAM_' + key.upper(): key for key in _SCOPE_KEYS | _META_KEYS}
_LIMIT = 32000
_WINDOWS = os.name == 'nt'


def _with_scheme(value: str) -> str:
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 2048:
        raise ValueError('Provide a bounded hostname or HTTP(S) URL')
    value = value.strip()
    if any(char.isspace() or ord(char) < 32 or char == '\\' for char in value):
        raise ValueError('URL contains invalid characters')
    if '://' in value:
        return value
    if value == '::1':
        value = '[::1]'
    parsed = urlsplit('//' + value)
    host = parsed.hostname or ''
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == 'localhost'
    return ('http://' if loopback else 'https://') + value


def normalize_origin(value: str) -> str:
    """Bare hosts use HTTPS; explicitly local loopback hosts use HTTP."""
    return origin(_with_scheme(value))


def _normalize_url(value: str) -> str:
    base, path = url_asset(_with_scheme(value))
    return base + path


class RedTeamDefaults:
    """Only scope preferences live here; credentials and shell syntax are rejected."""

    def __init__(self, project_root: str | Path):
        self.workspace = Workspace(project_root)
        self.root = self.workspace.root
        self.directory = self.root / '.local-coder'
        self.path = self.directory / 'redteam.defaults.env'

    def _private_directory(self) -> None:
        if self.directory.is_symlink():
            raise ValueError('Red team defaults directory cannot be a symlink')
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode) or (not _WINDOWS and info.st_uid != os.getuid()):
            raise ValueError('Defaults require an owner-controlled private directory')
        self.directory.chmod(0o700)
        if self.path.is_symlink():
            raise ValueError('Red team defaults file cannot be a symlink')

    def _factory(self) -> dict:
        return {
            'name': 'Review this project', 'objective': LOCAL_OBJECTIVE,
            'paths': ['src'] if (self.root / 'src').is_dir() else ['.'],
            'excluded_paths': [], 'origins': [], 'excluded_urls': [],
            'authorization': LOCAL_AUTHORIZATION, 'active_http': False, 'max_requests': 20,
            'preset': 'source-review', 'last_engagement_id': '',
        }

    @staticmethod
    def _parse_env(text: str) -> dict:
        if not isinstance(text, str) or len(text.encode()) > _LIMIT:
            raise ValueError('Defaults import must be text smaller than 32 KB')
        values = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            if not separator or key not in _ENV_KEYS or _ENV_KEYS[key] in values:
                raise ValueError('Use unique REDTEAM_ preference keys only; credentials and shell syntax are not accepted')
            try:
                values[_ENV_KEYS[key]] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError('Each defaults value must be JSON; this file is never sourced or interpolated') from exc
        return values

    @staticmethod
    def _serialize(values: dict) -> str:
        return '# Local source-review preferences. JSON values; never source this file.\n' + ''.join(
            key + '=' + json.dumps(values[name], ensure_ascii=False) + '\n'
            for key, name in sorted(_ENV_KEYS.items())
        )

    def _validated(self, values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - (_SCOPE_KEYS | _META_KEYS):
            raise ValueError('Only red team scope preferences can be saved; no credentials or arbitrary environment values')
        merged = self._factory() | values
        for key in ('paths', 'excluded_paths', 'origins', 'excluded_urls'):
            if not isinstance(merged[key], list) or any(not isinstance(value, str) or not value.strip() for value in merged[key]):
                raise ValueError(f'{key} must be a list of nonempty strings')
        merged['origins'] = [normalize_origin(value) for value in merged['origins']]
        merged['excluded_urls'] = [_normalize_url(value) for value in merged['excluded_urls']]
        for key in ('paths', 'excluded_paths'):
            paths = []
            for value in merged[key]:
                path = self.workspace.resolve(value)
                relative = path.relative_to(self.root)
                if any(part in {'.git', '.local-coder'} for part in relative.parts):
                    raise ValueError('Scope paths cannot target private agent or Git state')
                if key == 'paths' and not path.exists():
                    raise ValueError('In-scope paths must exist in this project')
                paths.append(relative.as_posix())
            merged[key] = list(dict.fromkeys(paths))
        scope = EngagementScope.model_validate({key: merged[key] for key in _SCOPE_KEYS}).model_dump()
        if scope['active_http'] and scope['authorization'].strip() == LOCAL_AUTHORIZATION:
            raise ValueError('HTTP observations need an explicit HTTP authorization reference; local source-review authorization does not apply')
        preset = merged['preset']
        if not isinstance(preset, str) or preset not in {'source-review', 'api-review'}:
            raise ValueError('Choose source-review or api-review')
        if preset == 'api-review' and not scope['origins']:
            raise ValueError('API review requires explicitly supplied allowed origins')
        identity = merged['last_engagement_id']
        if not isinstance(identity, str) or (identity and not re.fullmatch(r'eng-[a-f0-9]{12}', identity)):
            raise ValueError('Invalid last engagement ID')
        return scope | {'preset': preset, 'last_engagement_id': identity}

    def _write(self, values: dict) -> None:
        self._private_directory()
        text = self._serialize(values)
        if len(text.encode()) > _LIMIT:
            raise ValueError('Defaults exceed the 32 KB storage limit')
        descriptor, name = tempfile.mkstemp(prefix='.redteam-defaults-', dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
                if not _WINDOWS:
                    os.fchmod(stream.fileno(), 0o400)
            temporary.chmod(0o400)
            # Windows cannot atomically replace a destination with its read-only
            # attribute set. Only this validated ordinary file is made writable.
            if _WINDOWS and self.path.exists():
                if not self.path.is_file() or self.path.stat().st_nlink != 1:
                    raise ValueError('Defaults must be an ordinary file')
                self.path.chmod(0o600)
            try:
                os.replace(temporary, self.path)
            finally:
                if _WINDOWS and self.path.exists():
                    self.path.chmod(0o400)
        finally:
            if _WINDOWS and temporary.exists():
                temporary.chmod(0o600)
            temporary.unlink(missing_ok=True)

    def _load(self) -> dict:
        self._private_directory()
        if not self.path.exists():
            values = self._validated(self._factory())
            self._write(values)
            return values
        descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            private = (not info.st_mode & 0o222) if _WINDOWS else (
                info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o400)
            if (not stat.S_ISREG(info.st_mode) or not private
                    or info.st_nlink != 1 or info.st_size > _LIMIT):
                raise ValueError('Defaults must be an owner-only read-only regular file (0400), smaller than 32 KB')
            text = stream.read(_LIMIT + 1)
        return self._validated(self._parse_env(text))

    def status(self) -> dict:
        values = self._load()
        settings = {key: values[key] for key in _SCOPE_KEYS}
        return {
            'settings': settings, 'env_path': str(self.path), 'env_text': self._serialize(values),
            'exists': True, 'preset': values['preset'], 'last_engagement_id': values['last_engagement_id'],
            'presets': [
                {'id': 'source-review', 'name': 'Source review', 'description': 'Review this project locally. No HTTP requests.'},
                {'id': 'api-review', 'name': 'API review', 'description': 'Local source review with optional, explicitly authorized HTTP observations.'},
            ],
            'preview': {
                'valid': True, 'source_paths': settings['paths'], 'origins': settings['origins'],
                'active_http': settings['active_http'], 'network_requests_on_save': 0,
                'message': 'Scope preferences validated. Starting without a selected engagement always creates a local-only source review.',
            },
        }

    def update(self, updates: dict) -> dict:
        if not isinstance(updates, dict):
            raise ValueError('Defaults updates must be an object')
        current = self._load()
        if 'env_text' in updates:
            if set(updates) != {'env_text'}:
                raise ValueError('Import env_text on its own')
            candidate = self._factory() | self._parse_env(updates['env_text'])
        else:
            if set(updates) - (_SCOPE_KEYS | _META_KEYS | {'settings'}):
                raise ValueError('Only scope preferences can be saved')
            settings = updates.get('settings', {})
            if not isinstance(settings, dict) or set(settings) - _SCOPE_KEYS:
                raise ValueError('settings must contain only engagement scope fields')
            candidate = current | {key: value for key, value in updates.items() if key != 'settings'} | settings
        self._write(self._validated(candidate))
        return self.status()

    def ensure_engagement(self, store: EngagementStore) -> str:
        """Prepare a local-only scope; saved URL preferences never authorize HTTP."""
        if store.workspace.root != self.root:
            raise ValueError('Engagement store must belong to this project')
        values = self._load()
        scope_values = {key: values[key] for key in _SCOPE_KEYS}
        if scope_values['active_http'] or scope_values['origins'] or not scope_values['authorization'].strip():
            scope_values['authorization'] = LOCAL_AUTHORIZATION
        scope_values.update(active_http=False, origins=[], excluded_urls=[])
        scope = EngagementScope.model_validate(scope_values)
        # Reuse only an exact scope with an actual authorization statement.
        previous = values['last_engagement_id']
        candidates = ([{'id': previous}] if previous else []) + store.list()
        seen = set()
        for item in reversed(candidates) if not previous else candidates:
            identity = item['id']
            if identity in seen:
                continue
            seen.add(identity)
            try:
                record = store.load(identity)
            except (ValueError, OSError):
                continue
            if record['scope'] == scope.model_dump() and record['scope']['authorization'].strip():
                if previous != identity:
                    self.update({'last_engagement_id': identity})
                return identity
        record = store.create(scope)
        self.update({'last_engagement_id': record['id']})
        return record['id']


def ensure_local_engagement(store: EngagementStore, project_root: str | Path) -> str:
    return RedTeamDefaults(project_root).ensure_engagement(store)

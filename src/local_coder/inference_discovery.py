"""Bounded, cancellable filesystem metadata discovery; never executes candidates.

The protected dotenv manifest contains paths only. It is parsed as JSON values,
never sourced as shell code, and is not a tool capability to read external files.
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import struct
import threading
import time


_SKIP = {'proc', 'sys', 'dev', 'run'}
_SKIP_NAMES = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.npm', '.rustup', '.cargo', '.mypy_cache', '.pytest_cache'}
_SCALARS = {0: ('B', 1), 1: ('b', 1), 2: ('H', 2), 3: ('h', 2), 4: ('I', 4), 5: ('i', 4), 6: ('f', 4), 7: ('?', 1), 10: ('Q', 8), 11: ('q', 8), 12: ('d', 8)}
_WINDOWS = os.name == 'nt'
MAX_DISCOVERY_CACHE_ENTRIES = 4096
MAX_DISCOVERY_CACHE_BYTES = 4 * 1024 * 1024


def protected_manifest_path() -> Path:
    return Path.home() / '.config' / 'local-coder' / 'locations.env'


def read_locations_manifest(path=None) -> dict[str, str]:
    """Parse the path-only manifest as data; shell syntax is never evaluated."""
    full = Path(path) if path else protected_manifest_path()
    if any(parent.is_symlink() for parent in full.parents):
        raise ValueError('Location manifest cannot use symlink directories')
    if full.is_symlink():
        raise ValueError('Location manifest cannot be a symlink')
    if not full.exists():
        return {}
    info = full.stat()
    public = bool(info.st_mode & 0o222) if _WINDOWS else bool(stat.S_IMODE(info.st_mode) & 0o077)
    if not full.is_file() or info.st_size > 16 * 1048576 or info.st_nlink != 1 or public:
        raise ValueError('Location manifest must be a private regular file')
    values = {}
    with full.open('rb') as stream:
        encoded = stream.read(16 * 1048576 + 1)
    if len(encoded) > 16 * 1048576:
        raise ValueError('Location manifest exceeds the size limit')
    for row in encoded.decode('utf-8').splitlines():
        if not row or row.startswith('#'):
            continue
        key, separator, value = row.partition('=')
        if not separator or not re.fullmatch(r'LOCAL_CODER_(?:(?:MODEL|RUNTIME)_\d+|SELECTED_(?:MODEL_PATH|RUNTIME_PATH))', key):
            raise ValueError('Location manifest contains unsupported keys')
        decoded = json.loads(value)
        if not isinstance(decoded, str) or not Path(decoded).is_absolute() or '\0' in decoded:
            raise ValueError('Location manifest values must be absolute path strings')
        values[key] = decoded
    return values


def computer_roots() -> list[str]:
    if os.name == 'nt':
        return [f'{letter}:\\' for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(f'{letter}:\\').is_dir()]
    return ['/']


def recommended_roots() -> list[str]:
    roots = [Path.home(), Path('/opt'), Path('/usr/local'), Path('/usr/bin'), Path('/srv'), Path('/mnt'), Path('/media'), Path('/data'), Path('/models'), Path('/storage'), Path('/var/lib')]
    if os.name == 'nt':
        roots = [Path.home(), Path(os.environ.get('ProgramFiles', 'C:/Program Files')), Path('C:/models')]
    return [str(root) for root in roots if root.is_dir()]


def _regular(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError('Choose an absolute local path')
    candidate = candidate.resolve(strict=True)
    if any(candidate.is_relative_to(Path('/' + excluded)) for excluded in _SKIP):
        raise ValueError('Pseudo-filesystem candidates are excluded')
    if not stat.S_ISREG(candidate.stat().st_mode):
        raise ValueError('Candidate must be a regular local file')
    return candidate


@contextmanager
def open_regular_candidate(path: Path):
    """Open an already-resolved candidate without blocking on a replacement.

    Discovery intentionally supports selected model-file symlinks by resolving
    them before this helper. The final candidate must remain the same regular
    file throughout inspection. This is not an OS-level filesystem sandbox.
    """
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Candidate must be a regular local file')
        yield stream, before
        after, current = os.fstat(stream.fileno()), path.stat(follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (not stat.S_ISREG(current.st_mode)
                or any(getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(current, field)
                       for field in fields)):
            raise ValueError('Candidate changed during metadata inspection')


def inspect_gguf(path: str | Path) -> dict:
    """Read at most 1 MiB and traverse at most 16 MiB of GGUF metadata, no tensors."""
    full = _regular(path)
    size = full.stat().st_size
    result = {'path': str(full), 'name': full.name, 'size_bytes': size, 'valid': False, 'validity': 'invalid', 'metadata_partial': False, 'context_length': None, 'architecture': None, 'model_name': None, 'metadata_bytes_read': 0, 'trusted': False}
    consumed = 0
    with open_regular_candidate(full) as (stream, info):
        size = info.st_size
        result['size_bytes'] = size
        def read(count):
            nonlocal consumed
            if count < 0 or consumed + count > 1048576 or stream.tell() + count > min(size, 16 * 1048576):
                raise ValueError('GGUF metadata inspection bound reached')
            data = stream.read(count)
            consumed += len(data)
            if len(data) != count:
                raise ValueError('Truncated GGUF metadata')
            return data
        def skip(count):
            if count < 0 or stream.tell() + count > min(size, 16 * 1048576):
                raise ValueError('GGUF metadata inspection bound reached')
            stream.seek(count, 1)
        def string(keep=True):
            length = struct.unpack('<Q', read(8))[0]
            if keep:
                if length > 65536:
                    raise ValueError('GGUF metadata string exceeds bound')
                return read(length).decode('utf-8', errors='replace')
            skip(length)
            return None
        def value(kind, keep=False):
            if kind in _SCALARS:
                fmt, width = _SCALARS[kind]
                raw = read(width)
                return struct.unpack('<' + fmt, raw)[0] if keep else None
            if kind == 8:
                return string(keep)
            if kind == 9:
                subtype, count = struct.unpack('<IQ', read(12))
                if count > 1000000 or subtype == 9:
                    raise ValueError('GGUF metadata array exceeds bound')
                if subtype in _SCALARS:
                    skip(count * _SCALARS[subtype][1])
                elif subtype == 8:
                    for _ in range(count):
                        string(False)
                else:
                    raise ValueError('Unknown GGUF metadata array type')
                return None
            raise ValueError('Unknown GGUF metadata value type')
        try:
            magic, version, tensors, metadata_count = struct.unpack('<4sIQQ', read(24))
            if magic != b'GGUF' or version not in (2, 3) or metadata_count > 1000000 or tensors > 10000000:
                raise ValueError('Unsupported or invalid GGUF header')
            result.update(valid=tensors > 0, validity='header_valid' if tensors else 'vocabulary_only', version=version, tensor_count=tensors, header_valid=True)
            metadata = {}
            for _ in range(min(metadata_count, 4096)):
                key = string()
                kind = struct.unpack('<I', read(4))[0]
                keep = key in {'general.name', 'general.architecture'} or key.endswith('.context_length')
                item = value(kind, keep)
                if keep:
                    metadata[key] = item
                    if key == 'general.architecture' and isinstance(item, str):
                        result['architecture'] = item[:500]
                    if key == 'general.name' and isinstance(item, str):
                        result['model_name'] = item[:500]
                    if key.endswith('.context_length') and type(item) is int and 512 <= item <= 16777216:
                        result['context_length'] = item
            if metadata_count > 4096:
                result['metadata_partial'] = True
            result['architecture'] = metadata.get('general.architecture') if isinstance(metadata.get('general.architecture'), str) else None
            result['model_name'] = metadata.get('general.name') if isinstance(metadata.get('general.name'), str) else None
            for key, item in metadata.items():
                if key.endswith('.context_length') and type(item) is int and 512 <= item <= 16777216:
                    result['context_length'] = item
                    if key == f"{result['architecture']}.context_length":
                        break
            if not result['metadata_partial'] and tensors:
                result['validity'] = 'metadata_valid'
        except (ValueError, struct.error) as exc:
            if result.get('header_valid'):
                result['metadata_partial'] = True
            result['note'] = str(exc)
        result['metadata_bytes_read'] = consumed
    result['display_name'] = result['model_name'] or result['name']
    result['recommended_context_size'] = recommend_context(result)
    return result


def available_ram_bytes() -> int | None:
    try:
        if platform.system() == 'Linux':
            with Path('/proc/meminfo').open() as stream:
                for row in stream:
                    if row.startswith('MemAvailable:'):
                        return int(row.split()[1]) * 1024
        return int(os.sysconf('SC_AVPHYS_PAGES')) * int(os.sysconf('SC_PAGE_SIZE'))
    except (OSError, ValueError, AttributeError):
        return None


def recommend_context(model: dict, available_bytes: int | None = None) -> int:
    """Conservative CPU estimate, not a claimed model maximum or guaranteed fit."""
    native = model.get('context_length')
    maximum = min(native if type(native) is int and native >= 512 else 4096, 8192)
    available = available_ram_bytes() if available_bytes is None else available_bytes
    if available is not None:
        # Reserve model weights plus 30% RAM and estimate 512 KiB per context token.
        room = max(0, int(available * .7) - int(model.get('size_bytes', 0)))
        maximum = min(maximum, max(512, room // (512 * 1024)))
    result = 512
    while result * 2 <= maximum:
        result *= 2
    return result


def inspect_runtime(path: str | Path) -> dict:
    full = _regular(path)
    with open_regular_candidate(full) as (stream, info):
        size = info.st_size
        header = stream.read(64)
        kind = None
        architecture = None
        if header.startswith(b'\x7fELF') and len(header) >= 20 and header[5] in (1, 2):
            kind = 'ELF'
            architecture = {62: 'x64', 183: 'arm64', 3: 'x86'}.get(struct.unpack(('<' if header[5] == 1 else '>') + 'H', header[18:20])[0], 'unknown')
        elif header.startswith(b'MZ') and len(header) >= 64:
            offset = struct.unpack('<I', header[60:64])[0]
            if offset <= 1048576:
                stream.seek(offset)
                pe = stream.read(6)
                if len(pe) == 6 and pe.startswith(b'PE\0\0'):
                    kind = 'PE'
                    architecture = {0x8664: 'x64', 0xaa64: 'arm64', 0x14c: 'x86'}.get(struct.unpack('<H', pe[4:6])[0], 'unknown')
        elif header[:4] in (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
            kind = 'Mach-O'
    expected = 'PE' if os.name == 'nt' else 'Mach-O' if platform.system() == 'Darwin' else 'ELF'
    native_arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64' if platform.machine().lower() in ('x86_64', 'amd64') else 'unknown'
    executable = os.name == 'nt' or os.access(full, os.X_OK)
    compatible = kind == expected and architecture in (None, 'unknown', native_arch)
    libraries = []
    with os.scandir(full.parent) as entries:
        for index, entry in enumerate(entries):
            if index >= 10000:
                break
            name = entry.name.lower()
            if ('ggml' in name or 'llama' in name) and ('.so' in name or name.endswith(('.dll', '.dylib'))):
                try:
                    libraries.append({'name': entry.name, 'size_bytes': entry.stat().st_size})
                except OSError:
                    pass
                if len(libraries) >= 200:
                    break
    return {'path': str(full), 'name': full.name, 'size_bytes': size, 'format': kind, 'architecture': architecture, 'executable': executable, 'platform_compatible': compatible, 'valid': bool(kind and executable and compatible), 'validity': 'detected_not_trusted' if kind and executable and compatible else 'incompatible_or_invalid', 'trusted': False, 'health': 'not_started', 'libraries': libraries, 'note': 'Binary header only; health and required flags are checked only when you explicitly start the runtime.'}


class DiscoveryManager:
    def __init__(self, state_dir=None, manifest_path=None, max_entries=10000000, max_seconds=180):
        if (type(max_entries) is not int or not 1 <= max_entries <= 10000000
                or type(max_seconds) not in (int, float) or not math.isfinite(max_seconds)
                or not 0 < max_seconds <= 3600):
            raise ValueError('Discovery bounds must be positive finite limits within supported allowances')
        self.state_dir = Path(state_dir) if state_dir else None
        self.manifest_path = Path(manifest_path) if manifest_path else protected_manifest_path()
        self.max_entries, self.max_seconds = int(max_entries), float(max_seconds)
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._worker = None
        self._state = {'state': 'idle', 'started': False, 'models': [], 'runtimes': [], 'scanned_entries': 0, 'scanned_directories': 0, 'errors': 0, 'skipped': 0, 'truncated': False, 'limits': {'max_entries': self.max_entries, 'max_seconds': self.max_seconds}, 'manifest_path': str(self.manifest_path), 'computer_roots': computer_roots()}
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._selection = {}
        try:
            saved = read_locations_manifest(self.manifest_path)
            self._selection = {key.removeprefix('LOCAL_CODER_SELECTED_').lower(): value for key, value in saved.items() if key.startswith('LOCAL_CODER_SELECTED_')}
        except (OSError, ValueError):
            pass

    def status(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def start(self, roots=None):
        if roots is not None and (not isinstance(roots, list) or not roots or len(roots) > 100 or not all(isinstance(root, str) and Path(root).is_absolute() for root in roots)):
            raise ValueError('Discovery roots must be 1–100 absolute local directories')
        with self._lock:
            if self._worker and self._worker.is_alive():
                return self.status()
            selected = roots if roots is not None else recommended_roots()
            self._cancel = threading.Event()
            self._state.update(state='scanning', started=True, roots=selected, models=[], runtimes=[], scanned_entries=0, scanned_directories=0, errors=0, skipped=0, truncated=False, started_at=time.time(), current_path='')
            self._state.pop('error', None)
            self._worker = threading.Thread(target=self._scan, args=(selected, self._cancel), daemon=True, name='local-inference-discovery')
            self._worker.start()
            return self.status()

    def cancel(self):
        self._cancel.set()
        return self.status()

    close = cancel

    def _inspect_cached(self, full, model):
        def fingerprint():
            info = full.stat()
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        identity, key = fingerprint(), str(full)
        cached = self._cache.pop(key, None)
        if cached is not None:
            self._cache_bytes -= cached[2]
            if cached[0] == identity:
                self._cache[key] = cached
                self._cache_bytes += cached[2]
                return cached[1]
        record = inspect_gguf(full) if model else inspect_runtime(full)
        if fingerprint() != identity:
            raise ValueError('Candidate changed during metadata inspection')
        size = len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        if size <= MAX_DISCOVERY_CACHE_BYTES:
            self._cache[key] = (identity, record, size)
            self._cache_bytes += size
            while (len(self._cache) > MAX_DISCOVERY_CACHE_ENTRIES
                   or self._cache_bytes > MAX_DISCOVERY_CACHE_BYTES):
                _, removed = self._cache.popitem(last=False)
                self._cache_bytes -= removed[2]
        return record

    def _scan(self, roots, cancelled):
        started = time.monotonic()
        visited, paths = set(), set()
        stack = list(reversed(roots))
        models, runtimes = [], []
        try:
            while stack and not cancelled.is_set():
                if self._state['scanned_entries'] >= self.max_entries or time.monotonic() - started >= self.max_seconds:
                    with self._lock:
                        self._state['truncated'] = True
                    break
                folder = Path(stack.pop())
                try:
                    if folder.is_symlink():
                        continue
                    actual = folder.resolve(strict=True)
                    if actual in (Path('/proc'), Path('/sys'), Path('/dev'), Path('/run')):
                        continue
                    identity = (actual.stat().st_dev, actual.stat().st_ino)
                    if identity in visited:
                        continue
                    visited.add(identity)
                    with self._lock:
                        self._state.update(current_path=str(actual), scanned_directories=self._state['scanned_directories'] + 1)
                    with os.scandir(actual) as entries:
                        for entry in entries:
                            if cancelled.is_set():
                                break
                            if time.monotonic() - started >= self.max_seconds:
                                with self._lock:
                                    self._state['truncated'] = True
                                break
                            with self._lock:
                                self._state['scanned_entries'] += 1
                                if self._state['scanned_entries'] >= self.max_entries:
                                    self._state['truncated'] = True
                                    break
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name in _SKIP_NAMES or actual == Path('/') and entry.name in _SKIP:
                                    with self._lock:
                                        self._state['skipped'] += 1
                                else:
                                    stack.append(entry.path)
                                continue
                            if not (entry.name.lower().endswith('.gguf') or entry.name.lower() in ('llama-server', 'llama-server.exe')):
                                continue
                            try:
                                full = _regular(entry.path)
                                if str(full) in paths:
                                    continue
                                paths.add(str(full))
                                record = self._inspect_cached(full, entry.name.lower().endswith('.gguf'))
                                target = models if entry.name.lower().endswith('.gguf') else runtimes
                                target.append(record)
                                with self._lock:
                                    self._state['models' if target is models else 'runtimes'] = list(target)
                            except (OSError, ValueError, struct.error):
                                with self._lock:
                                    self._state['errors'] += 1
                except (OSError, ValueError):
                    with self._lock:
                        self._state['errors'] += 1
            with self._lock:
                self._state.update(state='cancelled' if cancelled.is_set() else 'complete', elapsed_seconds=round(time.monotonic() - started, 3), current_path='')
            self._manifest()
        except Exception as exc:
            with self._lock:
                self._state.update(state='error', error=f'Discovery failed: {type(exc).__name__}')

    def _manifest(self):
        destination = self.manifest_path
        directory = destination.parent
        # Reject redirected manifest directories; never follow user-controlled state links.
        current = directory
        while current != current.parent:
            if current.is_symlink():
                raise ValueError('Protected location manifest cannot use symlink directories')
            current = current.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        if destination.is_symlink():
            raise ValueError('Protected location manifest cannot be a symlink')
        with self._lock:
            entries = {f'LOCAL_CODER_MODEL_{index:05d}': item['path'] for index, item in enumerate(self._state['models']) if item['valid']}
            entries.update({f'LOCAL_CODER_RUNTIME_{index:05d}': item['path'] for index, item in enumerate(self._state['runtimes']) if item['valid']})
            entries.update({f'LOCAL_CODER_SELECTED_{key.upper()}': value for key, value in self._selection.items()})
        data = '# Local Coder locations only. Read as data; never source this file.\n' + ''.join(f'{key}={json.dumps(value, ensure_ascii=False)}\n' for key, value in entries.items())
        if len(data.encode()) > 16 * 1048576:
            raise ValueError('Location manifest exceeds 16 MiB; scan result remains available')
        temporary = directory / ('.locations-' + os.urandom(8).hex())
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                output.write(data)
            temporary.chmod(0o400)
            if _WINDOWS and destination.exists():
                if not destination.is_file() or destination.stat().st_nlink != 1:
                    raise ValueError('Location manifest must be an ordinary file')
                destination.chmod(0o600)
            try:
                os.replace(temporary, destination)
            finally:
                if destination.exists():
                    destination.chmod(0o400)
        finally:
            if _WINDOWS and temporary.exists():
                temporary.chmod(0o600)
            temporary.unlink(missing_ok=True)

    def select(self, runtime_path=None, model_path=None):
        result = {}
        if runtime_path is not None:
            record = inspect_runtime(runtime_path)
            if not record['valid']:
                raise ValueError('Selected runtime must have a compatible executable binary header')
            result['runtime_path'] = record['path']
        if model_path is not None:
            record = inspect_gguf(model_path)
            if not record['valid']:
                raise ValueError('Selected model must have a supported GGUF header')
            result.update(model_path=record['path'], context_size=record['recommended_context_size'])
        if not result:
            raise ValueError('Select a detected runtime or GGUF model')
        with self._lock:
            self._selection.update({key: value for key, value in result.items() if key.endswith('_path')})
        self._manifest()
        return result

"""Private, per-user discovery of authenticated workspace engines."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import stat

from local_coder.file_lock import exclusive_lock


def canonical_workspace(workspace):
    value = Path(workspace).resolve().as_posix()
    return value.lower() if os.name == 'nt' else value


def _connection_directory(directory, *, create=False):
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError('Engine connection directory must be absolute')
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError('Engine connection directory must not use symlinks')
    for folder in (directory.parent, directory):
        if create:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name != 'nt' and info.st_uid != os.getuid()):
            raise ValueError('Engine connection directory must belong to the current user')
    if create:
        directory.chmod(0o700)
    info = directory.stat()
    if os.name != 'nt' and stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Engine connection directory must be private')
    return directory, info


@contextmanager
def connection_directory_lock(directory):
    """Serialize publication and stale removal without ever replacing the lock.

    Only mutating operations call this helper. Diagnostics remain read-only.
    The private, owned regular lock is retained so every process locks the same
    inode; unsafe existing files are rejected before locking or writing.
    """
    directory, original = _connection_directory(directory)
    target = directory / '.publication.lock'
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    if target.is_symlink():
        raise ValueError('Engine publication lock must not be a symlink')
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, 'r+b') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != 'nt' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValueError('Engine publication lock must be a private owned regular file')
        with exclusive_lock(stream):
            _, latest = _connection_directory(directory)
            lock_info = target.lstat()
            if (latest.st_dev, latest.st_ino) != (original.st_dev, original.st_ino) or (lock_info.st_dev, lock_info.st_ino, lock_info.st_nlink) != (info.st_dev, info.st_ino, 1):
                raise ValueError('Engine publication lock or directory changed')
            yield


def publish_connection(workspace, origin, token):
    if not token:
        return None
    configured = os.environ.get('LOCAL_CODER_CONNECTION_DIR')
    directory = Path(configured) if configured else Path.home() / '.local-coder' / 'connections'
    directory, _ = _connection_directory(directory, create=True)
    canonical = canonical_workspace(workspace)
    target = directory / (hashlib.sha256(canonical.encode()).hexdigest() + '.json')
    value = {'workspace': canonical, 'origin': origin, 'token': token, 'pid': os.getpid()}
    with connection_directory_lock(directory):
        fd, temporary = tempfile.mkstemp(dir=directory, prefix='.connection-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(value, output)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return target

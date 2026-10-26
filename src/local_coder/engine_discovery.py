"""Private, per-user discovery of authenticated workspace engines."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def canonical_workspace(workspace):
    value = Path(workspace).resolve().as_posix()
    return value.lower() if os.name == 'nt' else value


def publish_connection(workspace, origin, token):
    if not token:
        return None
    parent = Path.home() / '.local-coder'
    directory = parent / 'connections'
    for folder in (parent, directory):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.is_symlink() or (os.name != 'nt' and folder.stat().st_uid != os.getuid()):
            raise ValueError('Engine connection directory must belong to the current user')
    directory.chmod(0o700)
    canonical = canonical_workspace(workspace)
    target = directory / (hashlib.sha256(canonical.encode()).hexdigest() + '.json')
    value = {'workspace': canonical, 'origin': origin, 'token': token, 'pid': os.getpid()}
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='.connection-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target

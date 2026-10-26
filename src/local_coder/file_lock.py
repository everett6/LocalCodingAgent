"""Advisory file locking on Windows and POSIX without extra dependencies."""

import os
from contextlib import contextmanager


@contextmanager
def exclusive_lock(file, blocking=True):
    if os.name == "nt":
        import msvcrt

        # Windows permits locks beyond EOF, including byte zero of an empty
        # file. Reading or initializing that byte before acquisition races a
        # competing handle's mandatory lock and can raise PermissionError.
        file.seek(0)
        msvcrt.locking(
            file.fileno(), (msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK), 1
        )
        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), (fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)))
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

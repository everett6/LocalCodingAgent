"""Advisory file locking on Windows and POSIX without extra dependencies."""
from contextlib import contextmanager
import os


@contextmanager
def exclusive_lock(file, blocking=True):
    if os.name == "nt":
        import msvcrt
        file.seek(0)
        if not file.read(1):
            file.write("\0")
            file.flush()
        file.seek(0)
        msvcrt.locking(file.fileno(), (msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK), 1)
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

"""File lock acquisition must not touch a byte another handle already owns."""

import io
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import local_coder.file_lock as locks


@pytest.mark.parametrize("blocking", [True, False])
def test_windows_acquisition_does_not_read_or_initialize_locked_byte(
    monkeypatch, blocking
):
    class LockedContents(io.BytesIO):
        def fileno(self):
            return 42

        def read(self, *args):
            raise PermissionError(
                "Another handle owns byte zero until locking succeeds"
            )

        def write(self, *args):
            raise PermissionError(
                "Sentinel initialization can race another lock holder"
            )

    file = LockedContents()
    calls = []

    def locking(descriptor, mode, length):
        assert file.tell() == 0
        calls.append((descriptor, mode, length))

    crt = SimpleNamespace(locking=locking, LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3)
    monkeypatch.setitem(sys.modules, "msvcrt", crt)
    monkeypatch.setattr(locks, "os", SimpleNamespace(name="nt"))
    with (
        pytest.raises(RuntimeError, match="body failed"),
        locks.exclusive_lock(file, blocking=blocking),
    ):
        file.seek(7)
        raise RuntimeError("body failed")
    assert calls == [(42, 1 if blocking else 2, 1), (42, 3, 1)]


def test_windows_failed_acquisition_does_not_unlock_an_unowned_region(monkeypatch):
    file = io.StringIO()
    calls = []

    def locking(descriptor, mode, length):
        calls.append(mode)
        raise PermissionError("region already locked")

    crt = SimpleNamespace(locking=locking, LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3)
    monkeypatch.setitem(sys.modules, "msvcrt", crt)
    monkeypatch.setattr(locks, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(file, "fileno", lambda: 42)
    with (
        pytest.raises(PermissionError, match="already locked"),
        locks.exclusive_lock(file, blocking=False),
    ):
        pytest.fail("A denied acquisition must not enter the protected section")
    assert calls == [crt.LK_NBLCK]


def test_native_lock_can_protect_an_empty_file_without_initializing_contents(tmp_path):
    path = tmp_path / "empty.lock"
    with path.open("a+", encoding="utf-8") as stream, locks.exclusive_lock(stream):
        assert path.stat().st_size == 0
    assert path.read_bytes() == b""


def test_native_blocking_waiter_serializes_competing_file_handles(tmp_path):
    path = tmp_path / "shared.lock"
    held = threading.Event()
    attempted = threading.Event()
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with path.open("a+", encoding="utf-8") as stream, locks.exclusive_lock(stream):
            held.set()
            assert release.wait(5)

    def waiter():
        with path.open("a+", encoding="utf-8") as stream:
            attempted.set()
            with locks.exclusive_lock(stream):
                acquired.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(holder)
        assert held.wait(3)
        second = executor.submit(waiter)
        assert attempted.wait(3)
        try:
            assert not acquired.wait(0.1)
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert acquired.is_set()

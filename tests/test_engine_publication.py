"""Publication and stale repair coordinate on a private, stable advisory lock."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading

import pytest

from local_coder import doctor, engine_discovery


def stale_descriptor(root, monkeypatch):
    directory = root / 'private' / 'connections'
    directory.mkdir(mode=0o700, parents=True)
    directory.parent.chmod(0o700)
    monkeypatch.setenv('LOCAL_CODER_CONNECTION_DIR', str(directory))
    target = doctor.descriptor_path(root)
    target.write_text(json.dumps({'workspace': str(root.resolve()), 'origin': 'http://127.0.0.1:1234', 'pid': 999999, 'token': 'fixture-private-value'}))
    target.chmod(0o600)
    return target


def test_publisher_waits_for_stale_repair_and_active_replacement_survives(tmp_path, monkeypatch):
    target = stale_descriptor(tmp_path, monkeypatch)
    entered, published = threading.Event(), threading.Event()
    errors, threads = [], []
    original_lock = engine_discovery.exclusive_lock
    @contextmanager
    def observed_lock(stream):
        if threading.current_thread().name == 'test-publisher':
            entered.set()
        with original_lock(stream):
            yield
    monkeypatch.setattr(engine_discovery, 'exclusive_lock', observed_lock)

    def publish():
        try:
            engine_discovery.publish_connection(tmp_path, 'http://127.0.0.1:5678', 'fresh-private-value')
            published.set()
        except Exception as exc:
            errors.append(exc)

    def pid_state(pid):
        if pid == os.getpid():
            return 'alive'
        thread = threading.Thread(target=publish, name='test-publisher')
        threads.append(thread)
        thread.start()
        assert entered.wait(1), 'Publisher never attempted the shared lock'
        assert not published.wait(.05), 'Publisher replaced descriptor while Doctor held the lock'
        return 'dead'
    monkeypatch.setattr(doctor, 'pid_state', pid_state)
    try:
        assert doctor.repair(tmp_path, 'clear_stale_engine_descriptor')
    finally:
        for thread in threads:
            thread.join(2)
    assert not errors and published.is_set()
    assert json.loads(target.read_text())['pid'] == os.getpid()
    assert json.loads(target.read_text())['origin'].endswith(':5678')
    with pytest.raises(ValueError, match='active or unknown'):
        doctor.repair(tmp_path, 'clear_stale_engine_descriptor')
    assert target.exists()
    if os.name != 'nt':
        assert (target.parent / '.publication.lock').stat().st_mode & 0o777 == 0o600


def test_repair_rereads_new_active_publication_instead_of_old_diagnostic(tmp_path, monkeypatch):
    target = stale_descriptor(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, 'pid_state', lambda pid: 'alive' if pid == os.getpid() else 'dead')
    assert doctor.engine_diagnostic(tmp_path)['pid_state'] == 'dead'
    engine_discovery.publish_connection(tmp_path, 'http://127.0.0.1:5678', 'fresh-private-value')
    with pytest.raises(ValueError, match='active or unknown'):
        doctor.repair(tmp_path, 'clear_stale_engine_descriptor')
    assert json.loads(target.read_text())['pid'] == os.getpid()


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'public', 'foreign-owner', 'fifo'])
def test_unsafe_existing_lock_never_changes_descriptor_or_referenced_file(tmp_path, monkeypatch, unsafe):
    if unsafe in {'fifo', 'public', 'foreign-owner'} and os.name == 'nt':
        pytest.skip('POSIX filesystem ownership and modes')
    target = stale_descriptor(tmp_path, monkeypatch)
    lock = target.parent / '.publication.lock'
    original = target.read_bytes()
    secret = tmp_path / 'private-file'
    secret.write_bytes(b'preserve fixture private file')
    secret.chmod(0o600)
    if unsafe == 'symlink':
        lock.symlink_to(secret)
    elif unsafe == 'hardlink':
        os.link(secret, lock)
    elif unsafe == 'fifo':
        os.mkfifo(lock)
    else:
        lock.write_bytes(b'')
        lock.chmod(0o644 if unsafe == 'public' else 0o600)
        if unsafe == 'foreign-owner':
            real_fstat = os.fstat
            def foreign(fd):
                info = real_fstat(fd)
                if info.st_ino == lock.stat().st_ino:
                    values = list(info)
                    values[4] = os.getuid() + 1
                    return os.stat_result(values)
                return info
            monkeypatch.setattr(os, 'fstat', foreign)
    monkeypatch.setattr(doctor, 'pid_state', lambda pid: 'dead')
    for operation in (
        lambda: doctor.repair(tmp_path, 'clear_stale_engine_descriptor'),
        lambda: engine_discovery.publish_connection(tmp_path, 'http://127.0.0.1:5678', 'fresh-private-value'),
    ):
        with pytest.raises((ValueError, OSError)):
            operation()
        assert target.read_bytes() == original
        assert secret.read_bytes() == b'preserve fixture private file'
    if unsafe == 'public':
        assert lock.stat().st_mode & 0o777 == 0o644


def test_doctor_read_does_not_create_lock_or_other_state(tmp_path, monkeypatch):
    target = stale_descriptor(tmp_path, monkeypatch)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*'))
    monkeypatch.setattr(doctor, 'pid_state', lambda pid: 'dead')
    assert doctor.engine_diagnostic(tmp_path)['pid_state'] == 'dead'
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*')) == before
    assert not (target.parent / '.publication.lock').exists()


def test_publisher_refuses_symlinked_connection_directory(tmp_path, monkeypatch):
    actual = tmp_path / 'actual'; actual.mkdir(mode=0o700)
    redirected = tmp_path / 'redirected'; redirected.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv('LOCAL_CODER_CONNECTION_DIR', str(redirected))
    with pytest.raises(ValueError, match='symlink'):
        engine_discovery.publish_connection(tmp_path, 'http://127.0.0.1:1234', 'fixture')
    assert not list(actual.iterdir())

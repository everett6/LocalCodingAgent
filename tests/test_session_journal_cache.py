"""Warm journal reads skip reparsing without hiding edits or mutable changes."""
from concurrent.futures import ThreadPoolExecutor
import os

import pytest

import local_coder.orchestrator.sessions as module
from local_coder.orchestrator.sessions import SessionError, SessionStore

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows journal caching is conservatively disabled")


def seeded(tmp_path):
    store = SessionStore(str(tmp_path))
    store.save("kept", request="before", events=[{"message": "original", "data": {"value": [1, 2]}}])
    return store


def count_parses(store, monkeypatch):
    original = store._read_json
    calls = []

    def counted(path, limit):
        if path == store.path:
            calls.append(path)
        return original(path, limit)

    monkeypatch.setattr(store, "_read_json", counted)
    return calls


def test_warm_get_list_reads_parse_once_and_return_defensive_copies(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    calls = count_parses(store, monkeypatch)
    for _ in range(8):
        record = store.get("kept")
        record["events"][0]["message"] = "changed by reader"
        record["events"][0]["data"]["value"].append(3)
        listed = store.list()
        assert listed[0]["events"][0]["message"] == "original"
        assert listed[0]["events"][0]["data"]["value"] == [1, 2]
        listed[0]["events"].clear()
    assert len(calls) == 1
    assert store.get("kept")["events"]


def test_warm_get_copies_only_the_requested_record(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    store.save("other", request="unrelated", events=[{"message": "large unrelated report"}])
    store.list()
    copied = []
    original = module.copy.deepcopy

    def counted(value):
        copied.append(value)
        return original(value)

    monkeypatch.setattr(module.copy, "deepcopy", counted)
    assert store.get("kept")["request"] == "before"
    assert len(copied) == 1 and copied[0]["session_id"] == "kept"


def test_external_atomic_replacement_invalidates_the_validated_snapshot(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    calls = count_parses(store, monkeypatch)
    assert store.get("kept")["request"] == "before"
    SessionStore(str(tmp_path)).save("kept", request="after!")
    assert store.get("kept")["request"] == "after!"
    assert len(calls) == 2


def test_same_size_raw_edit_with_restored_mtime_still_invalidates_via_ctime(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    calls = count_parses(store, monkeypatch)
    store.get("kept")
    before = store.path.stat()
    original = store.path.read_bytes()
    changed = original.replace(b'"before"', b'"after!"')
    assert len(changed) == len(original)
    store.path.write_bytes(changed)
    os.utime(store.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert store.get("kept")["request"] == "after!"
    assert len(calls) == 2


@pytest.mark.parametrize("raw", [b"{damaged", b"[]", b"null"])
def test_corruption_never_reuses_a_previous_valid_snapshot(tmp_path, raw):
    store = seeded(tmp_path)
    store.get("kept")
    store.path.write_bytes(raw)
    with pytest.raises(SessionError):
        store.list()
    assert store._journal_cache is None
    assert store.path.read_bytes() == raw


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_unsafe_replacement_cannot_return_cached_history(tmp_path, kind):
    store = seeded(tmp_path)
    original = store.path.read_bytes()
    store.get("kept")
    store.path.unlink()
    if kind in {"symlink", "hardlink"}:
        target = tmp_path / "other.json"
        target.write_bytes(original)
        try:
            store.path.symlink_to(target) if kind == "symlink" else os.link(target, store.path)
        except OSError:
            pytest.skip("file links unavailable")
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(store.path)
    else:
        store.path.mkdir()
    with pytest.raises(SessionError):
        store.get("kept")
    assert store._journal_cache is None


def test_replaced_state_directory_is_checked_before_cache_reuse(tmp_path):
    store = seeded(tmp_path)
    store.get("kept")
    backup = tmp_path / "state-backup"
    store.state_root.rename(backup)
    try:
        store.state_root.symlink_to(backup, target_is_directory=True)
    except OSError:
        pytest.skip("directory links unavailable")
    with pytest.raises(SessionError):
        store.list()
    assert store._journal_cache is None


def test_absent_journal_invalidates_cached_history_and_can_start_empty(tmp_path):
    store = seeded(tmp_path)
    store.get("kept")
    store.path.unlink()
    assert store.list() == []
    assert store._journal_cache is None
    store.save("new", request="new history")
    assert store.get("kept") is None
    assert store.get("new")["request"] == "new history"


def test_failed_write_does_not_publish_mutated_cached_records(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    store.get("kept")
    original = store.path.read_bytes()

    def fail(*args, **kwargs):
        raise SessionError("synthetic write failure")

    with monkeypatch.context() as patch:
        patch.setattr(module, "_write_json_atomic", fail)
        with pytest.raises(SessionError):
            store.save("kept", request="must not leak")
    assert store.path.read_bytes() == original
    assert store.get("kept")["request"] == "before"


def test_serialized_cache_limit_keeps_larger_journals_uncached(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    monkeypatch.setattr(module, "MAX_JOURNAL_CACHE_BYTES", store.path.stat().st_size - 1)
    calls = count_parses(store, monkeypatch)
    assert store.get("kept")["request"] == "before"
    assert store.get("kept")["request"] == "before"
    assert len(calls) == 2 and store._journal_cache is None


def test_zero_cache_cap_uses_the_uncached_validation_path(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    monkeypatch.setattr(module, "MAX_JOURNAL_CACHE_BYTES", 0)
    calls = count_parses(store, monkeypatch)
    for _ in range(3):
        assert store.get("kept")["request"] == "before"
    assert len(calls) == 3 and store._journal_cache is None


@pytest.mark.skipif(os.name == "nt" or getattr(os, "getuid", lambda: 0)() == 0,
                    reason="POSIX unprivileged read permission contract")
def test_revoked_read_permission_cannot_return_previously_cached_data(tmp_path):
    store = seeded(tmp_path)
    store.get("kept")
    store.path.chmod(0)
    try:
        with pytest.raises(SessionError):
            store.get("kept")
        assert store._journal_cache is None
    finally:
        store.path.chmod(0o600)


def test_external_replacement_during_a_cache_hit_retries_with_new_data(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    store.get("kept")
    fingerprint = store._journal_fingerprint
    checks = []

    def raced():
        identity = fingerprint()
        checks.append(identity)
        if len(checks) == 1:
            changed = store.path.read_bytes().replace(b'"before"', b'"after!"')
            replacement = store.path.parent / "replacement.json"
            replacement.write_bytes(changed)
            os.replace(replacement, store.path)
        return identity

    monkeypatch.setattr(store, "_journal_fingerprint", raced)
    assert store.get("kept")["request"] == "after!"


def test_repeated_changes_during_validation_fail_closed(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    store.get("kept")
    fingerprint = store._journal_fingerprint
    counter = []

    def unstable():
        identity = fingerprint()
        counter.append(None)
        return (*identity[:-1], identity[-1] + len(counter))

    monkeypatch.setattr(store, "_journal_fingerprint", unstable)
    with pytest.raises(SessionError, match="changed repeatedly"):
        store.list()
    assert store._journal_cache is None


def test_shared_instance_and_external_writer_keep_cache_consistent(tmp_path):
    store = seeded(tmp_path)
    store.list()

    def work(number):
        if number % 4 == 0:
            SessionStore(str(tmp_path)).save(f"task-{number}", request="fixture")
        else:
            store.list()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(work, range(20)))
    assert len(store.list()) == 6

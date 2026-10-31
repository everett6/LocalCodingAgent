"""Watcher readiness, source provenance and cache bounds are observable contracts."""
import json
import os
import threading

import pytest

from local_coder.context.code_index import CodeIndex, INDEX_RELPATH, INDEX_VERSION


def test_startup_refresh_happens_after_watch_subscription(tmp_path, monkeypatch):
    import watchfiles
    subscribed = threading.Event()
    index = CodeIndex(str(tmp_path), persist=False)
    refresh = index.refresh

    def checked_refresh():
        assert subscribed.is_set(), "refresh before subscription leaves an edit-loss gap"
        return refresh()

    def watch(*args, **kwargs):
        assert kwargs["yield_on_timeout"] is True
        subscribed.set()
        (tmp_path / "startup.py").write_text("def subscription_marker():\n    pass\n")
        yield set()
        while not kwargs["stop_event"].wait(.01):
            yield set()

    monkeypatch.setattr(index, "refresh", checked_refresh)
    monkeypatch.setattr(watchfiles, "watch", watch)
    index.start_watching()
    try:
        assert index.search("subscription marker", refresh=False)
        assert index.watcher_error is None
    finally:
        index.stop_watching()


def test_watch_subscription_failure_is_not_reported_as_ready(tmp_path, monkeypatch):
    import watchfiles
    index = CodeIndex(str(tmp_path), persist=False)

    def broken(*args, **kwargs):
        raise OSError("synthetic subscription failure")

    monkeypatch.setattr(watchfiles, "watch", broken)
    with pytest.raises(RuntimeError, match="failed to initialize"):
        index.start_watching()
    assert index._watch_thread is None


def test_private_paths_excluded_on_initial_refresh_and_event_updates(tmp_path):
    (tmp_path / ".env").write_text("PRIVATE_ENV_CANARY=example")
    (tmp_path / ".env.local").write_text("PRIVATE_ENV_CANARY=example")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "keys.py").write_text("PRIVATE_SSH_CANARY=example")
    (tmp_path / "credentials.json").write_text('{"PRIVATE_CREDENTIAL_CANARY":"example"}')
    (tmp_path / "app.py").write_text("def ordinary_marker():\n    pass\n")
    index = CodeIndex(str(tmp_path), persist=False)
    index.refresh()
    index.update_paths({tmp_path / ".env", tmp_path / ".ssh/keys.py", tmp_path / "credentials.json"})
    assert index.file_count == 1
    assert not index.search("PRIVATE", refresh=False)
    assert index.search("ordinary marker", refresh=False)


def test_unsafe_or_malformed_cache_entries_are_not_loaded(tmp_path):
    path = tmp_path / INDEX_RELPATH
    path.parent.mkdir(parents=True)
    for data in ([], {"version": INDEX_VERSION, "files": []},
                 {"version": INDEX_VERSION, "files": {"bad.py": {"chunks": [["bad"]]}}}):
        path.write_text(json.dumps(data))
        (tmp_path / "real.py").write_text("def current_marker():\n    pass\n")
        index = CodeIndex(str(tmp_path))
        assert index.search("current marker")


def test_snippets_do_not_mix_new_source_with_old_symbol_locations(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def previous_marker():\n    return 1\n")
    index = CodeIndex(str(tmp_path), persist=False)
    hits = index.search("previous marker")
    assert hits and index.file_hashes(["app.py"])["app.py"] == hits[0].source_hash
    source.write_text("def replacement_marker():\n    return 2\n")
    index.add_snippets(hits, "previous marker")
    assert hits == []


def test_generation_is_independent_of_discovery_insertion_order(tmp_path):
    (tmp_path / "a.py").write_text("def first_marker():\n    pass\n")
    (tmp_path / "b.py").write_text("def second_marker():\n    pass\n")
    first = CodeIndex(str(tmp_path), persist=False)
    second = CodeIndex(str(tmp_path), persist=False)
    first.update_paths({"a.py"})
    first.update_paths({"b.py"})
    second.update_paths({"b.py"})
    second.update_paths({"a.py"})
    assert first.generation == second.generation


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special file")
def test_index_source_reader_rejects_fifo_without_waiting_for_writer(tmp_path):
    source = tmp_path / "blocked.py"
    os.mkfifo(source)
    index = CodeIndex(str(tmp_path), persist=False)
    assert index._read_source(source) is None
    assert index.refresh()["updated"] == 0


def test_explicit_refresh_detects_same_size_mtime_content_edits(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def previous_marker():\n    return 1\n")
    index = CodeIndex(str(tmp_path), persist=False)
    index.refresh()
    before = source.stat()
    generation = index.generation
    source.write_text("def replaced_marker():\n    return 2\n")
    assert source.stat().st_size == before.st_size
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert index.refresh()["updated"] == 1
    assert index.generation != generation
    assert not index.search("previous", refresh=False)


def test_refresh_drops_previously_indexed_path_replaced_by_symlink(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def previous_marker():\n    return 1\n")
    index = CodeIndex(str(tmp_path), persist=False)
    index.refresh()
    source.unlink()
    (tmp_path / "other.py").write_text("def replaced_marker():\n    return 2\n")
    source.symlink_to(tmp_path / "other.py")
    index.refresh()
    assert "app.py" not in index.file_hashes(["app.py"])


def test_concurrent_starts_wait_for_same_subscription_readiness(tmp_path, monkeypatch):
    import watchfiles
    subscribed = threading.Event()
    release = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    index = CodeIndex(str(tmp_path), persist=False)
    errors = []

    def watch(*args, **kwargs):
        subscribed.set()
        release.wait(2)
        yield set()
        while not kwargs["stop_event"].wait(.01):
            yield set()

    def start(done):
        try:
            index.start_watching()
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(watchfiles, "watch", watch)
    first = threading.Thread(target=start, args=(first_done,))
    second = threading.Thread(target=start, args=(second_done,))
    first.start()
    try:
        assert subscribed.wait(1)
        second.start()
        assert not second_done.wait(.05)
        release.set()
        assert first_done.wait(2) and second_done.wait(2)
        assert not errors
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
        index.stop_watching()

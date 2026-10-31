"""Session corruption and unsafe state must fail closed without losing history."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat

import pytest

import local_coder.orchestrator.sessions as module
from local_coder.orchestrator.sessions import SessionError, SessionStore


@pytest.mark.parametrize("content", [b"", b"{partial", b"[]", b"null", b"42", b'{"old": null}',
                                     b'{"old":{"session_id":"other","updated_at":"today"}}',
                                     b'{"old":{"session_id":"old","updated_at":3}}',
                                     b'{"old":{},"old":{}}', b'{"old":NaN}', b'\xff'])
def test_invalid_index_is_not_replaced_by_next_save_or_create(tmp_path, content):
    store = SessionStore(str(tmp_path))
    store.path.write_bytes(content)
    for operation in (lambda: store.save("new", request="fixture"), lambda: store.create("new"), store.list):
        with pytest.raises(SessionError):
            operation()
        assert store.path.read_bytes() == content
    assert not (store.sessions_dir / "new" / "state.json").exists()


def test_read_failure_is_not_interpreted_as_an_empty_journal(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path))
    store.save("old", request="saved history")
    original = store.path.read_bytes()

    def denied(*args, **kwargs):
        raise PermissionError("synthetic read denial")

    monkeypatch.setattr(module, "read_source_snapshot", denied)
    with pytest.raises(SessionError):
        store.save("new", request="must not replace old")
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("payload", [[], None, {"turns": []},
                                     {"version": 1, "session_id": "other", "turns": [], "created_at": "x", "updated_at": "x"}])
def test_invalid_persistent_state_is_never_recreated(tmp_path, payload):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    session.state_path.write_text(json.dumps(payload))
    original = session.state_path.read_bytes()
    with pytest.raises(SessionError):
        store.open_or_create("kept")
    assert session.state_path.read_bytes() == original


@pytest.mark.parametrize("changes", [{"index": True}, {"request": []}, {"checkpoints": []}, {"attempts": False},
                                     {"report": {}}, {"context": []}, {"status": None}])
def test_invalid_turn_shapes_are_rejected_on_open(tmp_path, changes):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    session.start_turn("valid request")
    state = json.loads(session.state_path.read_text())
    state["turns"][0].update(changes)
    session.state_path.write_text(json.dumps(state))
    original = session.state_path.read_bytes()
    with pytest.raises(SessionError):
        store.open("kept")
    assert session.state_path.read_bytes() == original


@pytest.mark.parametrize("name", ["sessions.json", "sessions.lock", "sessions"])
def test_state_symlinks_cannot_redirect_index_or_persistent_writes(tmp_path, name):
    state = tmp_path / ".local-coder"
    state.mkdir()
    if name == "sessions":
        target = tmp_path / "unrelated-folder"
        target.mkdir()
        original = None
    else:
        target = tmp_path / "notes.json"
        target.write_text("{}")
        original = target.read_bytes()
    (state / name).symlink_to(target)
    with pytest.raises(SessionError):
        store = SessionStore(str(tmp_path))
        if name == "sessions":
            store.create("kept")
        else:
            store.save("kept", request="must stay in state")
    if original is not None:
        assert target.read_bytes() == original
    else:
        assert list(target.iterdir()) == []


def test_session_state_symlinks_and_hardlinks_are_rejected(tmp_path):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    target = tmp_path / "document.json"
    target.write_bytes(session.state_path.read_bytes())
    session.state_path.unlink()
    session.state_path.symlink_to(target)
    with pytest.raises(SessionError):
        store.open_or_create("kept")
    session.state_path.unlink()
    try:
        os.link(target, session.state_path)
    except OSError:
        pytest.skip("hardlinks unavailable")
    with pytest.raises(SessionError):
        store.open("kept")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special files only")
@pytest.mark.parametrize("name", ["sessions.json", "sessions.lock", "sessions/kept/state.json"])
def test_special_state_files_are_rejected_before_open(tmp_path, name):
    target = tmp_path / ".local-coder" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(target)
    with pytest.raises(SessionError):
        store = SessionStore(str(tmp_path))
        if name.endswith("state.json"):
            store.open_or_create("kept")
        else:
            store.save("kept", request="fixture")
    assert stat.S_ISFIFO(target.stat().st_mode)


def test_corruption_after_open_prevents_state_and_index_replacement(tmp_path):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    session.start_turn("first")
    state = session.state_path.read_bytes()
    store.path.write_bytes(b"{damaged index")
    with pytest.raises(SessionError):
        session.start_turn("must not overwrite saved checkpoint")
    assert session.state_path.read_bytes() == state
    assert store.path.read_bytes() == b"{damaged index"


def test_failed_reload_releases_the_acquired_session_lease(tmp_path):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    original = session.state_path.read_bytes()
    session.state_path.write_bytes(b"{bad reload")
    with pytest.raises(SessionError):
        session.acquire()
    session.state_path.write_bytes(original)
    other = store.open("kept")
    other.acquire()
    other.release()


def test_atomic_write_failure_preserves_old_bytes_and_removes_owned_temporary_file(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path))
    store.save("old", request="saved history")
    original = store.path.read_bytes()

    def failed_replace(*args, **kwargs):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(module.os, "replace", failed_replace)
    with pytest.raises(SessionError):
        store.save("new", request="must not replace old")
    assert store.path.read_bytes() == original
    assert list(store.path.parent.glob(".session-*.tmp")) == []


def test_byte_limits_and_non_json_values_preserve_current_journal(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path))
    store.save("old", request="saved history")
    original = store.path.read_bytes()
    monkeypatch.setattr(module, "MAX_JOURNAL_BYTES", len(original) + 20)
    with pytest.raises(SessionError, match="byte limit"):
        store.save("new", request="界" * 100)
    assert store.path.read_bytes() == original
    for value in (object(), float("nan"), float("inf")):
        with pytest.raises(SessionError):
            store.save("old", metadata=value)
        assert store.path.read_bytes() == original


def test_concurrent_index_updates_do_not_lose_other_sessions(tmp_path):
    store = SessionStore(str(tmp_path))
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda number: SessionStore(str(tmp_path)).save(f"task-{number}", request="fixture"), range(20)))
    assert len(store.list()) == 20
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory mode contract")
def test_custom_state_preserves_preexisting_project_ancestor_permissions(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir(mode=0o755)
    metadata.chmod(0o755)
    store = SessionStore(str(tmp_path), state_dir="metadata/state")
    session = store.create("kept")
    session.start_turn("fixture")
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o755
    assert stat.S_IMODE(store.state_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(session.dir.stat().st_mode) == 0o700


def test_event_append_and_progress_do_not_follow_symlinks(tmp_path):
    session = SessionStore(str(tmp_path)).create("kept")
    session.start_turn("fixture")
    target = tmp_path / "notes.txt"
    target.write_text("unrelated project document")
    session.events_path.symlink_to(target)
    session.record_event({"event_type": "phase", "message": "must not append"})
    assert target.read_text() == "unrelated project document"
    with pytest.raises(SessionError):
        session.progress()


def test_event_limits_preserve_existing_log_and_progress_reads_a_bounded_tail(tmp_path, monkeypatch):
    session = SessionStore(str(tmp_path)).create("kept")
    session.start_turn("fixture")
    session.record_event({"event_type": "phase", "message": "old"})
    original = session.events_path.read_bytes()
    monkeypatch.setattr(module, "MAX_EVENT_BYTES", 64)
    session.record_event({"event_type": "phase", "message": "x" * 1000})
    assert session.events_path.read_bytes() == original
    monkeypatch.setattr(module, "MAX_EVENT_FILE_BYTES", len(original))
    session.record_event({"message": "next"})
    assert session.events_path.read_bytes() == original
    session.events_path.write_text("padding" * 100 + "\nnull\n[]\nnot-json\n" + json.dumps({"turn": 0, "event_type": "phase", "message": "latest"}) + "\n")
    monkeypatch.setattr(module, "EVENT_TAIL_BYTES", 250)
    progress = session.progress()
    assert progress["current_phase"] is None
    assert progress["observed_phase"] == "latest"
    assert progress["event_history_truncated"] is True


def test_append_cap_is_disclosed_after_reopen_even_below_event_tail_limit(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    session.start_turn("fixture")
    session.record_event({"event_type": "phase", "message": "saved"})
    original = session.events_path.read_bytes()
    assert len(original) < module.EVENT_TAIL_BYTES
    monkeypatch.setattr(module, "MAX_EVENT_FILE_BYTES", len(original))
    session.record_event({"event_type": "phase", "message": "omitted"})
    assert session.events_path.read_bytes() == original
    record = json.loads(session.events_status_path.read_text())
    assert record["omitted_events_at_least"] == 1 and record["reason"] == "event_file_limit"
    reopened = store.open("kept")
    assert reopened.progress()["event_history_truncated"] is True
    assert reopened.progress()["current_phase"] is None
    assert reopened.progress()["observed_phase"] == "saved"
    if os.name != "nt":
        assert stat.S_IMODE(session.events_status_path.stat().st_mode) == 0o600


def test_omitted_first_event_has_durable_disclosure_without_creating_an_event_log(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    session.start_turn("fixture")
    monkeypatch.setattr(module, "MAX_EVENT_BYTES", 32)
    session.record_event({"message": "oversized fixture" * 10})
    assert not session.events_path.exists()
    assert store.open("kept").progress()["event_history_truncated"] is True


def test_capped_event_history_cannot_move_a_completed_checkpoint_phase_backwards(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    turn = session.start_turn("fixture")
    session.record_event({"event_type": "phase", "message": "Exploring repository"})
    saved_events = session.events_path.read_bytes()
    monkeypatch.setattr(module, "MAX_EVENT_FILE_BYTES", len(saved_events))
    session.checkpoint(turn).put("execute", {"task_id": "execute", "status": "completed", "summary": "implemented"})
    session.record_event({"event_type": "phase", "message": "Reviewing changes"})

    for candidate in (session, store.open("kept")):
        progress = candidate.progress()
        assert progress["current_phase"] == "execute"
        assert progress["observed_phase"] == "Exploring repository"
        assert progress["event_history_truncated"] is True
        assert "execute" in progress["completed_phases"]
        assert progress["task_counts"]["completed"] == 1
    assert session.events_path.read_bytes() == saved_events


def test_complete_event_history_can_show_the_current_live_phase_after_checkpoint(tmp_path):
    session = SessionStore(str(tmp_path)).create("kept")
    turn = session.start_turn("fixture")
    session.checkpoint(turn).put("execute", {"status": "completed"})
    session.record_event({"event_type": "phase", "message": "Reviewing changes"})
    progress = session.progress()
    assert progress["current_phase"] == "Reviewing changes"
    assert progress["observed_phase"] == "Reviewing changes"
    assert progress["event_history_truncated"] is False


def test_omission_marker_never_replaces_invalid_metadata_or_follows_a_link(tmp_path, monkeypatch):
    session = SessionStore(str(tmp_path)).create("kept")
    session.start_turn("fixture")
    session.events_status_path.write_bytes(b"{existing damaged metadata")
    monkeypatch.setattr(module, "MAX_EVENT_BYTES", 32)
    session.record_event({"message": "oversized fixture" * 10})
    assert session.events_status_path.read_bytes() == b"{existing damaged metadata"
    with pytest.raises(SessionError):
        session.progress()
    session.events_status_path.unlink()
    target = tmp_path / "unrelated.json"
    target.write_text("{}")
    session.events_status_path.symlink_to(target)
    session.record_event({"message": "oversized fixture" * 10})
    assert target.read_text() == "{}"
    with pytest.raises(SessionError):
        session.progress()


def test_event_credentials_are_scrubbed_without_erasing_numeric_usage(tmp_path):
    session = SessionStore(str(tmp_path)).create("kept")
    session.start_turn("fixture")
    known_format = "sk-" + "x" * 40
    named_secret = "synthetic-password-fixture"
    session.record_event({"event_type": "model_error", "message": f"failed {known_format} {named_secret}",
                          "data": {"password": named_secret, "api_key": "another-synthetic-fixture",
                                   "input_tokens": 123, "output_tokens": 45}})
    encoded = session.events_path.read_text()
    assert known_format not in encoded and named_secret not in encoded
    assert "another-synthetic-fixture" not in encoded
    event = json.loads(encoded)
    assert event["data"]["input_tokens"] == 123 and event["data"]["output_tokens"] == 45
    assert event["data"]["api_key"] == "[redacted]"


def test_event_free_text_assignments_and_basic_headers_use_existing_secret_filters(tmp_path):
    session = SessionStore(str(tmp_path)).create("kept")
    session.start_turn("fixture")
    session.record_event({"message": "password=synthetic-fixture-only Basic c3ludGhldGljOmZpeHR1cmU="})
    encoded = session.events_path.read_text()
    assert "synthetic-fixture-only" not in encoded
    assert "c3ludGhldGljOmZpeHR1cmU=" not in encoded


def test_follow_up_history_bounds_requests_while_preserving_saved_originals(tmp_path):
    store = SessionStore(str(tmp_path))
    session = store.create("kept")
    request = "synthetic request " * 6000
    for _ in range(6):
        turn = session.start_turn(request)
        session.finish_turn(turn, "completed", report="result " * 1000)
    context = session.history_context()
    assert len(context) < 20_000
    assert context.count("full turn retained") == 5
    reopened = store.open("kept")
    assert all(turn["request"] == request for turn in reopened.turns)


@pytest.mark.parametrize("session_id", [None, 3, True, "bad\n", "../bad"])
def test_index_identifiers_reject_non_strings_and_noncanonical_names(tmp_path, session_id):
    store = SessionStore(str(tmp_path))
    with pytest.raises(SessionError):
        store.save(session_id, request="fixture")
    assert not store.path.exists()

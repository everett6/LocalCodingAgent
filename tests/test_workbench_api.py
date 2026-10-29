import asyncio
import subprocess
import threading
import time
from types import SimpleNamespace

from local_coder.git.checkpoints import CheckpointManager
from local_coder.remote import RemoteControlServer
from local_coder.types import AgentEvent
from local_coder.workbench import workspace_fingerprint


def test_settings_never_return_provider_secrets(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    status, settings = server.handle("POST", "/settings", {"backend": "anthropic", "model_id": "claude-test",
        "base_url": "https://api.anthropic.com/v1", "api_key": "private-key-123"})
    assert status == 200
    assert settings["provider"]["key_set"]
    assert "private-key-123" not in str(settings)
    coordinator = server._coordinator()
    assert coordinator.config.models["workbench"].resolve_api_key() == "private-key-123"
    assert server.handle("POST", "/settings", {"backend": "anthropic", "model_id": "c", "base_url": "http://evil.test/v1", "api_key": "private"})[0] == 400


def test_background_jobs_persist_results_and_isolate_events(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    class FakeCoordinator:
        async def run(self, request, **kwargs):
            server._event(AgentEvent(source="CODER", event_type="phase", message="Working"))
            return "Verified result"
        class Manager:
            async def close_all(self):
                pass
        model_manager = Manager()
    monkeypatch.setattr(server, "_coordinator", lambda: FakeCoordinator())
    status, job = server.handle("POST", "/jobs", {"request": "Fix boundaries", "mode": "bug_hunting"})
    assert status == 202
    for _ in range(100):
        _, record = server.handle("GET", f"/jobs/{job['session_id']}")
        if record["status"] not in {"queued", "running"}:
            break
        time.sleep(.01)
    assert record["status"] == "completed"
    assert record["result"] == "Verified result"
    _, events = server.handle("GET", f"/events?session_id={job['session_id']}&since=0")
    assert events["events"]
    assert all(e["session_id"] == job["session_id"] for e in events["events"])
    assert not server.handle("GET", "/events?session_id=unrelated")[1]["events"]


def test_jobs_can_be_cancelled_and_overlapping_mutations_are_rejected(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    started = threading.Event()
    class FakeCoordinator:
        async def run(self, request, **kwargs):
            started.set()
            await asyncio.sleep(20)
        class Manager:
            async def close_all(self):
                pass
        model_manager = Manager()
    monkeypatch.setattr(server, "_coordinator", lambda: FakeCoordinator())
    _, job = server.handle("POST", "/jobs", {"request": "Long job"})
    assert started.wait(2)
    assert server.handle("POST", "/jobs", {"request": "Conflicting job"})[0] == 409
    assert server.handle("POST", f"/jobs/{job['session_id']}/cancel", {})[0] == 200
    for _ in range(100):
        record = server.sessions.get(job["session_id"])
        if record["status"] == "cancelled":
            break
        time.sleep(.01)
    assert record["status"] == "cancelled"


def test_workspace_file_reads_are_bounded_to_project(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "app.py").write_text("print('hello')")
    server = RemoteControlServer(str(root))
    assert server.handle("GET", "/file?path=app.py")[1]["content"] == "print('hello')"
    assert server.handle("GET", "/file?path=../outside")[0] == 400


def test_undo_endpoint_restores_latest_saved_checkpoint(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    tracked = tmp_path / "app.py"
    tracked.write_text("before\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-qm", "initial")
    manager = CheckpointManager(str(tmp_path))
    checkpoint = manager.create()
    tracked.write_text("after\n", encoding="utf-8")
    server = RemoteControlServer(str(tmp_path))
    server.sessions.save(
        "session-1",
        status="completed",
        pre_run_checkpoint_id=checkpoint.checkpoint_id,
        workspace_fingerprint=workspace_fingerprint(str(tmp_path)),
    )

    status, result = server.handle("POST", "/undo", {"session_id": "session-1"})

    assert status == 200
    assert result["checkpoint_id"] == checkpoint.checkpoint_id
    assert tracked.read_text(encoding="utf-8") == "before\n"


def test_undo_endpoint_refuses_when_a_job_is_running(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    assert server._job_lock.acquire(blocking=False)
    try:
        status, result = server.handle("POST", "/undo", {"session_id": "session-1"})
    finally:
        server._job_lock.release()

    assert status == 409
    assert "active task" in result["error"]


def test_job_failure_is_persisted(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    def fail():
        raise RuntimeError("model configuration unavailable")
    monkeypatch.setattr(server, "_coordinator", fail)
    _, job = server.handle("POST", "/jobs", {"request": "Test"})
    for _ in range(100):
        record = server.sessions.get(job["session_id"])
        if record["status"] == "failed":
            break
        time.sleep(.01)
    assert record["status"] == "failed"
    assert "configuration" in record["error"]


def test_failed_coding_job_can_opt_into_checkpoint_rollback(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    target = tmp_path / "app.py"
    target.write_text("before\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-qm", "initial")
    server = RemoteControlServer(str(tmp_path))

    class FakeCoordinator:
        config = SimpleNamespace(verification=SimpleNamespace(rollback_on_failure=True))

        async def run(self, request, **kwargs):
            checkpoint = CheckpointManager(str(tmp_path)).create()
            server._event(AgentEvent(
                source="ORCHESTRATOR", event_type="checkpoint_created",
                message="checkpoint", data={"checkpoint_id": checkpoint.checkpoint_id},
            ))
            target.write_text("partial work\n", encoding="utf-8")
            server._event(AgentEvent(
                source="ORCHESTRATOR", event_type="task_status",
                message="failed", data={"status": "failed"},
            ))
            return "Verification failed"

        class Manager:
            async def close_all(self):
                pass

        model_manager = Manager()

    monkeypatch.setattr(server, "_coordinator", lambda: FakeCoordinator())
    _, job = server.handle("POST", "/jobs", {"request": "Fix app", "mode": "coding"})
    record = None
    for _ in range(100):
        record = server.sessions.get(job["session_id"])
        if record and record["status"] not in {"queued", "running"}:
            break
        time.sleep(.01)

    assert record is not None
    assert record["status"] == "failed"
    assert record["rollback_applied"] is True
    assert "restored to the pre-run checkpoint" in record["result"]
    assert target.read_text(encoding="utf-8") == "before\n"


def test_failure_status_survives_event_buffer_eviction(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    class FakeCoordinator:
        async def run(self, request, **kwargs):
            server._event(AgentEvent(source="ORCHESTRATOR", event_type="task_status", message="failed", data={"status": "failed"}))
            for _ in range(210):
                server._event(AgentEvent(source="TESTER", event_type="phase", message="Reviewing"))
            return "Tests passed, implementation failed"
        class Manager:
            async def close_all(self):
                pass
        model_manager = Manager()
    monkeypatch.setattr(server, "_coordinator", lambda: FakeCoordinator())
    _, job = server.handle("POST", "/jobs", {"request": "Fail implementation"})
    for _ in range(200):
        record = server.sessions.get(job["session_id"])
        if record["status"] not in {"queued", "running"}:
            break
        time.sleep(.01)
    assert record["status"] == "failed"


def test_restart_exposes_last_safe_checkpoint_before_job_finishes(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    server.sessions.save('interrupted-job', request='Inspect', status='running')
    server._context.session_id = 'interrupted-job'
    server._jobs['interrupted-job'] = {}
    server._event(AgentEvent(source='SECURITY', event_type='checkpoint_saved',
                            message='Saved complete round', data={'path': '.local-coder/checkpoint_test.json'}))
    restarted = RemoteControlServer(str(tmp_path))
    restarted.reconcile_sessions()
    record = restarted.sessions.get('interrupted-job')
    assert record['status'] == 'interrupted'
    assert record['checkpoint'] == '.local-coder/checkpoint_test.json'
    assert record['events'][0]['event_type'] == 'checkpoint_saved'


def test_workspace_engine_lease_rejects_second_server(tmp_path):
    import pytest
    first = RemoteControlServer(str(tmp_path)).make_http_server('127.0.0.1', 0)
    second = RemoteControlServer(str(tmp_path))
    try:
        with pytest.raises(ValueError, match='already has an engine'):
            second.make_http_server('127.0.0.1', 0)
    finally:
        first.server_close()
    next_server = second.make_http_server('127.0.0.1', 0)
    next_server.server_close()


def test_engine_owns_and_stops_native_index_watcher(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    http_server = server.make_http_server("127.0.0.1", 0)
    try:
        assert server.code_index._watch_thread is not None
        assert server.code_index._watch_thread.is_alive()
    finally:
        http_server.server_close()

    assert server.code_index._watch_thread is None


def test_shutdown_cancels_and_awaits_job_cleanup(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    started, cleaned = threading.Event(), threading.Event()
    class FakeCoordinator:
        async def run(self, request, **kwargs):
            try:
                started.set()
                await asyncio.sleep(30)
            finally:
                cleaned.set()
        class Manager:
            async def close_all(self):
                pass
        model_manager = Manager()
    monkeypatch.setattr(server, '_coordinator', lambda: FakeCoordinator())
    _, job = server.handle('POST', '/jobs', {'request': 'Run'})
    assert started.wait(2)
    status, _ = server.handle('POST', '/shutdown', {})
    assert status == 200
    assert cleaned.is_set()
    assert server.sessions.get(job['session_id'])['status'] == 'cancelled'
    assert server.handle('POST', '/jobs', {'request': 'Too late'})[0] == 503

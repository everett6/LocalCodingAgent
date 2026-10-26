import asyncio
import threading
import time

from local_coder.remote import RemoteControlServer
from local_coder.types import AgentEvent


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

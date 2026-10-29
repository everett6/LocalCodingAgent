"""Workbench completion must include cleanup, without making provider calls."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from local_coder.remote import RemoteControlServer
from local_coder.providers.quota import ProviderQuotaExhausted
from local_coder.types import AgentMode


def test_job_remains_running_until_resource_cleanup_finishes(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    cleaning, release = threading.Event(), threading.Event()

    class Coordinator:
        async def run(self, request, **kwargs):
            return "Verified result"

        class Manager:
            async def close_all(self):
                cleaning.set()
                await asyncio.to_thread(release.wait, 3)

        model_manager = Manager()

    monkeypatch.setattr(server, "_coordinator", Coordinator)
    _, job = server.handle("POST", "/jobs", {"request": "Inspect local sources"})
    assert cleaning.wait(3)
    try:
        assert server.sessions.get(job["session_id"])["status"] == "running"
        assert server.handle("POST", "/jobs", {"request": "Next task"})[0] == 409
    finally:
        release.set()
        server.drain_jobs()


def test_cleanup_failure_is_persisted_instead_of_false_completion(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))

    class Coordinator:
        async def run(self, request, **kwargs):
            return "Partial report"

        class Manager:
            async def close_all(self):
                raise RuntimeError("cleanup unavailable")

        model_manager = Manager()

    monkeypatch.setattr(server, "_coordinator", Coordinator)
    session_id = "cleanup-failure"
    job = {"cancel": threading.Event(), "loop": None, "task": None}
    server.sessions.save(session_id, request="Inspect", status="queued")
    server._jobs[session_id] = job
    server._job_lock.acquire()
    server._job_thread(session_id, job, "Inspect", AgentMode.CODING, "run", ".", False)
    record = server.sessions.get(session_id)
    assert record["status"] == "failed"
    assert "cleanup" in record["error"].lower()
    assert record["result"] == "Partial report"
    assert not server._job_lock.locked()


def test_sessions_retain_project_goal_across_followup_and_resume(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    goal = server.goal_ledger.create("Verify parser changes")
    observed = []

    class Coordinator:
        async def run(self, request, **kwargs):
            observed.append((server._context.goal_id, request))
            return "Report"

        async def resume_checkpoint(self, path, **kwargs):
            observed.append((server._context.goal_id, path))
            return SimpleNamespace(summary="Step resumed", status=SimpleNamespace(value="completed"))

        class Manager:
            async def close_all(self):
                pass

        model_manager = Manager()

    monkeypatch.setattr(server, "_coordinator", Coordinator)
    parent = None
    for options in ({"goal_id": goal["id"]}, {}, {"resume_checkpoint": ".local-coder/checkpoint_safe.json"}):
        status, job = server.handle("POST", "/jobs", {
            "request": "Continue parser", "parent_session_id": parent, **options,
        })
        assert status == 202
        assert server._job_lock.acquire(timeout=3)
        server._job_lock.release()
        record = server.sessions.get(job["session_id"])
        assert record["goal_id"] == goal["id"]
        assert observed[-1][0] == goal["id"]
        parent = job["session_id"]
    assert "Goal resume briefing" in observed[1][1]
    status, detached = server.handle("POST", "/jobs", {
        "request": "Separate task", "parent_session_id": parent, "goal_id": None,
    })
    assert status == 202
    assert server._job_lock.acquire(timeout=3)
    server._job_lock.release()
    assert server.sessions.get(detached["session_id"])["goal_id"] is None
    assert observed[-1][0] is None


def test_resume_rejects_goal_from_another_project(tmp_path):
    (tmp_path / "first").mkdir()
    (tmp_path / "other").mkdir()
    first = RemoteControlServer(str(tmp_path / "first"))
    other = RemoteControlServer(str(tmp_path / "other"))
    goal = first.goal_ledger.create("Private first-project objective")
    status, _ = other.handle("POST", "/jobs", {
        "request": "Resume saved step", "goal_id": goal["id"],
        "resume_checkpoint": ".local-coder/checkpoint_safe.json",
    })
    assert status == 400
    assert other.sessions.list() == []


@pytest.mark.parametrize("goal_id", [False, 0, [], {}, ""])
def test_job_rejects_malformed_goal_identity(tmp_path, monkeypatch, goal_id):
    server = RemoteControlServer(str(tmp_path))
    monkeypatch.setattr(server, "_coordinator", lambda: None)
    assert server.handle("POST", "/jobs", {"request": "Inspect", "goal_id": goal_id})[0] == 400
    assert server.sessions.list() == []


def test_cleanup_failure_preserves_quota_recovery_status_and_checkpoint(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))

    class Coordinator:
        async def run(self, request, **kwargs):
            raise ProviderQuotaExhausted({"message": "Daily allowance exhausted", "remaining": 0})

        class Manager:
            async def close_all(self):
                raise RuntimeError("cleanup unavailable")

        model_manager = Manager()

    monkeypatch.setattr(server, "_coordinator", Coordinator)
    session_id = "quota-cleanup-failure"
    job = {"cancel": threading.Event(), "loop": None, "task": None,
           "checkpoint": ".local-coder/checkpoint_safe.json"}
    server.sessions.save(session_id, request="Inspect", status="queued")
    server._jobs[session_id] = job
    server._job_lock.acquire()
    server._job_thread(session_id, job, "Inspect", AgentMode.CODING, "run", ".", False)
    record = server.sessions.get(session_id)
    assert record["status"] == "quota_exhausted"
    assert record["checkpoint"] == ".local-coder/checkpoint_safe.json"
    assert record["quota_notice"]["remaining"] == 0
    assert "cleanup" in record["error"].lower()


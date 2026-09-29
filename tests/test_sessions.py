"""Persistent sessions: on-disk turns and checkpoints, Coordinator resume,
and the CLI's --resume / --session / resume / sessions commands."""
import asyncio
import json

import pytest
from click.testing import CliRunner

from local_coder.cli.main import cli
from local_coder.orchestrator import coordinator as coordinator_module
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.orchestrator.sessions import SessionError, SessionStore
from local_coder.types import (
    AgentEvent, AgentResponse, AgentRole, AgentTask, ProjectConfig, TaskPlan, TaskStatus,
)


def ok(task_id="t", summary="done"):
    return AgentResponse(task_id=task_id, status=TaskStatus.COMPLETED, summary=summary)


# === SessionStore / Session ===

def test_session_persists_turns_and_checkpoints_across_store_instances(tmp_path):
    store = SessionStore(str(tmp_path))
    session = store.create("work-1")
    turn = session.start_turn("add a helper")
    session.checkpoint(turn).put("explore", "found utils.py")

    reopened = SessionStore(str(tmp_path)).open("work-1")

    assert reopened.turns[0]["request"] == "add a helper"
    assert reopened.checkpoint(reopened.turns[0]).get("explore") == "found utils.py"
    assert reopened.unfinished_turn()["index"] == 0
    state = json.loads((tmp_path / ".local-coder" / "sessions" / "work-1" / "state.json").read_text())
    assert state["turns"][0]["phase"] == "explore"
    # The shared index (used by the web UI and `sessions`) sees it too.
    assert store.get("work-1")["status"] == "running"


def test_follow_up_turn_carries_earlier_results_as_context(tmp_path):
    session = SessionStore(str(tmp_path)).create()
    first = session.start_turn("add a helper")
    session.finish_turn(first, "completed", report="Added helper() to utils.py")

    second = session.start_turn("now test it")

    assert "add a helper" in second["context"]
    assert "Added helper() to utils.py" in second["context"]
    assert session.unfinished_turn() is second


def test_new_request_supersedes_an_unfinished_turn(tmp_path):
    session = SessionStore(str(tmp_path)).create()
    first = session.start_turn("first")
    session.finish_turn(first, "interrupted")

    session.start_turn("second")

    assert session.turns[0]["status"] == "superseded"


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", ".hidden", "x" * 65])
def test_session_ids_cannot_escape_the_sessions_folder(tmp_path, bad_id):
    with pytest.raises(SessionError):
        SessionStore(str(tmp_path)).open_or_create(bad_id)


def test_latest_returns_the_most_recent_persistent_session(tmp_path):
    store = SessionStore(str(tmp_path))
    store.save("remote-only", request="x", phase="run")  # index-only record, not resumable
    older = store.create("older")
    older.start_turn("a")
    newer = store.create("newer")
    newer.start_turn("b")

    assert store.latest().session_id == "newer"


def test_session_lock_blocks_a_second_writer(tmp_path):
    store = SessionStore(str(tmp_path))
    session = store.create("busy")
    with session:
        with pytest.raises(SessionError, match="already running"):
            store.open("busy").acquire()
    store.open("busy").acquire()  # free again once released


def test_events_are_appended_to_the_session_log(tmp_path):
    session = SessionStore(str(tmp_path)).create()
    session.start_turn("x")
    session.record_event(AgentEvent(source="CODER", event_type="tool_called", message="read_file"))

    lines = session.events_path.read_text().splitlines()
    assert json.loads(lines[0])["message"] == "read_file"
    assert json.loads(lines[0])["turn"] == 0


# === Coordinator resume ===

class DictCheckpoint:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        json.dumps(value)  # must stay JSON-serializable to reach disk
        self.data[key] = value


def make_coordinator(tmp_path):
    return Coordinator(config=ProjectConfig(project_root=str(tmp_path)), project_root=str(tmp_path))


def test_rerun_with_checkpoint_skips_finished_phases(tmp_path):
    coordinator = make_coordinator(tmp_path)
    calls = []
    review_fails = [True]

    async def explore(request):
        calls.append("explore")
        return "exploration"

    async def plan(request, exploration):
        calls.append("plan")
        return TaskPlan(objective=request, tasks=[AgentTask(role=AgentRole.CODER, objective=request)])

    async def execute(request, exploration):
        calls.append("execute")
        return ok(summary="edited")

    async def review(result):
        calls.append("review")
        if review_fails[0]:
            raise RuntimeError("model server went away")
        return "looks fine"

    async def tests():
        calls.append("tests")
        return ok()

    coordinator._explore, coordinator._plan = explore, plan
    coordinator._execute_simple, coordinator._review, coordinator._run_tests = execute, review, tests
    checkpoint = DictCheckpoint()

    with pytest.raises(RuntimeError):
        asyncio.run(coordinator.run("add a helper", checkpoint=checkpoint))
    assert set(checkpoint.data) == {"explore", "plan", "execute"}

    calls.clear()
    review_fails[0] = False
    report = asyncio.run(coordinator.run("add a helper", checkpoint=checkpoint))

    assert calls == ["review", "tests"]
    assert "edited" in report
    assert {"review", "tests.0"} <= set(checkpoint.data)


def test_history_is_given_to_the_agents(tmp_path):
    coordinator = make_coordinator(tmp_path)
    seen = {}

    async def explore(request):
        seen["explore"] = request
        return "exploration"

    async def plan(request, exploration):
        return TaskPlan(objective=request, tasks=[AgentTask(role=AgentRole.CODER, objective=request)])

    async def execute(request, exploration):
        return ok()

    async def review(result):
        return "ok"

    coordinator._explore, coordinator._plan, coordinator._execute_simple = explore, plan, execute
    coordinator._review = review
    coordinator.config.verification.run_tests_after_changes = False

    asyncio.run(coordinator.run("now test it", history="Earlier in this session: added helper()"))

    assert "added helper()" in seen["explore"]
    assert seen["explore"].endswith("Current request: now test it")


def test_plan_tasks_finished_before_an_interruption_are_not_rerun(tmp_path, monkeypatch):
    coordinator = make_coordinator(tmp_path)
    executed = []
    fail_second = [True]

    class FakeAgent:
        async def execute(self, task):
            executed.append(task.task_id)
            if task.task_id == "t2" and fail_second[0]:
                raise RuntimeError("crashed")
            return ok(task.task_id)

    monkeypatch.setattr(coordinator_module, "create_agent", lambda *a, **k: FakeAgent())
    coordinator.model_manager.get_model = lambda *a, **k: asyncio.sleep(0, result=None)
    plan = TaskPlan(objective="x", tasks=[
        AgentTask(task_id="t1", role=AgentRole.CODER, objective="one"),
        AgentTask(task_id="t2", role=AgentRole.CODER, objective="two", depends_on=["t1"]),
    ])
    checkpoint = DictCheckpoint()

    first = asyncio.run(coordinator._execute_plan(plan, checkpoint))
    assert first.status == TaskStatus.FAILED
    assert set(checkpoint.data) == {"task.t1"}

    executed.clear()
    fail_second[0] = False
    second = asyncio.run(coordinator._execute_plan(plan, checkpoint))

    assert executed == ["t2"]
    assert second.status == TaskStatus.COMPLETED


# === CLI ===

class FakeCoordinator:
    """Replaces Coordinator.run: records its arguments and checkpoints a
    phase, failing once when told to."""
    runs = []
    fail_next = False

    def __init__(self, *args, **kwargs):
        pass

    def on_event(self, handler):
        pass

    async def run(self, request, checkpoint=None, history=""):
        FakeCoordinator.runs.append({
            "request": request, "history": history,
            "had_explore": checkpoint.get("explore") is not None,
        })
        if checkpoint.get("explore") is None:
            checkpoint.put("explore", "explored")
        if FakeCoordinator.fail_next:
            FakeCoordinator.fail_next = False
            raise RuntimeError("model crashed")
        return f"Report for {request}"


@pytest.fixture
def fake_coordinator(monkeypatch):
    FakeCoordinator.runs = []
    FakeCoordinator.fail_next = False
    monkeypatch.setattr(coordinator_module, "Coordinator", FakeCoordinator)
    return FakeCoordinator


def invoke(tmp_path, *args):
    return CliRunner().invoke(cli, ["--project", str(tmp_path), *args])


def test_cli_resume_picks_up_a_failed_run_from_its_checkpoint(tmp_path, fake_coordinator):
    fake_coordinator.fail_next = True
    failed = invoke(tmp_path, "add a helper")
    assert failed.exit_code != 0
    assert "--session local-" in failed.output

    resumed = invoke(tmp_path, "--resume")

    assert resumed.exit_code == 0, resumed.output
    assert "Report for add a helper" in resumed.output
    assert fake_coordinator.runs[-1] == {"request": "add a helper", "history": "", "had_explore": True}
    session = SessionStore(str(tmp_path)).latest()
    assert session.turns[0]["status"] == "completed"
    assert session.turns[0]["attempts"] == 2


def test_cli_follow_up_request_continues_the_session(tmp_path, fake_coordinator):
    invoke(tmp_path, "--session", "auth", "add login")
    result = invoke(tmp_path, "--resume", "now add logout")

    assert result.exit_code == 0, result.output
    assert "add login" in fake_coordinator.runs[-1]["history"]
    assert "Report for add login" in fake_coordinator.runs[-1]["history"]
    assert [t["request"] for t in SessionStore(str(tmp_path)).open("auth").turns] == ["add login", "now add logout"]


def test_cli_separate_runs_without_resume_start_separate_sessions(tmp_path, fake_coordinator):
    invoke(tmp_path, "first request")
    invoke(tmp_path, "second request")

    assert fake_coordinator.runs[-1]["history"] == ""
    assert len(SessionStore(str(tmp_path)).list()) == 2


def test_cli_resume_command_refuses_a_finished_session(tmp_path, fake_coordinator):
    invoke(tmp_path, "--session", "done", "add login")

    result = invoke(tmp_path, "resume", "done")

    assert result.exit_code != 0
    assert "Nothing to resume" in result.output


def test_cli_resume_command_resumes_by_id(tmp_path, fake_coordinator):
    fake_coordinator.fail_next = True
    invoke(tmp_path, "--session", "s1", "add login")

    result = invoke(tmp_path, "resume", "s1")

    assert result.exit_code == 0, result.output
    assert fake_coordinator.runs[-1]["had_explore"] is True


def test_cli_resume_without_sessions_explains(tmp_path, fake_coordinator):
    result = invoke(tmp_path, "--resume")

    assert result.exit_code != 0
    assert "No saved session" in result.output


def test_cli_sessions_lists_and_shows_turns(tmp_path, fake_coordinator):
    fake_coordinator.fail_next = True
    invoke(tmp_path, "--session", "s1", "add login")

    listing = invoke(tmp_path, "sessions")
    detail = invoke(tmp_path, "sessions", "s1")

    assert listing.exit_code == 0 and "s1" in listing.output and "failed" in listing.output
    assert detail.exit_code == 0 and "add login" in detail.output and "explore" in detail.output
    assert "local-coder resume s1" in detail.output


def test_cli_runs_a_request_given_directly(tmp_path, fake_coordinator):
    """`local-coder "Add OAuth login"` used to fail with "No such command"."""
    result = invoke(tmp_path, "Add OAuth login")

    assert result.exit_code == 0, result.output
    assert fake_coordinator.runs[-1]["request"] == "Add OAuth login"


def test_cli_single_word_typo_is_still_an_unknown_command(tmp_path, fake_coordinator):
    result = invoke(tmp_path, "staus")

    assert result.exit_code != 0
    assert "No such command" in result.output
    assert fake_coordinator.runs == []


def test_cli_ctrl_c_marks_the_turn_interrupted_and_resumable(tmp_path, fake_coordinator, monkeypatch):
    async def interrupted(self, request, checkpoint=None, history=""):
        checkpoint.put("explore", "explored")
        raise KeyboardInterrupt

    monkeypatch.setattr(FakeCoordinator, "run", interrupted)
    result = invoke(tmp_path, "--session", "s1", "add login")

    assert result.exit_code != 0
    turn = SessionStore(str(tmp_path)).open("s1").unfinished_turn()
    assert turn["status"] == "interrupted"
    assert turn["checkpoints"] == {"explore": "explored"}


def test_remote_run_uses_a_persistent_session_the_cli_can_resume(tmp_path, monkeypatch):
    from local_coder import remote

    FakeCoordinator.runs = []
    FakeCoordinator.fail_next = True
    monkeypatch.setattr(remote, "Coordinator", FakeCoordinator)
    server = remote.RemoteControlServer(str(tmp_path))

    status, payload = server.handle("POST", "/run", {"request": "add login", "session_id": "auth-run"})
    assert status == 500 and payload["session_id"] == "auth-run"

    monkeypatch.setattr(coordinator_module, "Coordinator", FakeCoordinator)
    result = invoke(tmp_path, "resume", "auth-run")
    assert result.exit_code == 0, result.output
    assert FakeCoordinator.runs[-1]["had_explore"] is True

    status, payload = server.handle("POST", "/run", {"request": "add logout", "session_id": "auth-run"})
    assert status == 200 and payload["turns"] == 2
    assert "add login" in FakeCoordinator.runs[-1]["history"]

    status, payload = server.handle("POST", "/run", {"request": "x", "session_id": "../etc"})
    assert status == 400

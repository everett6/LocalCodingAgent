"""Host deadlines cover setup and execution without replaying timed-out work."""
import asyncio

import pytest

from local_coder.orchestrator.blocked import AgentBlocked
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.types import AgentResponse, AgentRole, AgentTask, ProjectConfig, TaskPlan, TaskStatus


def setup_attempt(tmp_path, monkeypatch, stage):
    coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup = []
    calls = []

    async def step(name, value):
        calls.append(name)
        if stage == name:
            entered.set()
            try:
                await release.wait()
            finally:
                await asyncio.sleep(0)
                cleanup.append(name)
        return value

    async def model_for(*args, **kwargs):
        return await step("routing", object())

    async def repo_map(*args, **kwargs):
        return await step("context", "")

    async def drafter():
        return await step("draft", None)

    class Agent:
        async def execute(self, task):
            return await step("agent", AgentResponse(task_id=task.task_id,
                status=TaskStatus.COMPLETED, summary="fixture complete", tests_passed=True))

    monkeypatch.setattr(coordinator, "_model_for", model_for)
    monkeypatch.setattr(coordinator, "_repo_map", repo_map)
    monkeypatch.setattr(coordinator, "_get_drafter", drafter)
    monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent", lambda *a, **kw: Agent())
    # The public protocol stores integer seconds. Copy in a tiny deadline to
    # exercise the same host path without slow multi-second regression tests.
    task = AgentTask(task_id="deadline-fixture", role=AgentRole.CODER, objective="fixture only").model_copy(
        update={"timeout_seconds": .02})
    return coordinator, task, entered, cleanup, calls


@pytest.mark.parametrize("operation", ["dag", "simple"])
@pytest.mark.parametrize("stage", ["routing", "context", "draft", "agent"])
def test_task_deadline_covers_setup_and_agent_and_awaits_cleanup(tmp_path, monkeypatch, operation, stage):
    async def scenario():
        coordinator, task, entered, cleanup, calls = setup_attempt(tmp_path, monkeypatch, stage)
        events = []
        coordinator.on_event(events.append)
        if operation == "dag":
            result = await asyncio.wait_for(coordinator._attempt_dag_task(task, False), .3)
        else:
            result = await asyncio.wait_for(coordinator._execute_planned_simple(task, "fixture", ""), .3)
        assert entered.is_set()
        assert cleanup == [stage]
        assert result.status == TaskStatus.BLOCKED
        assert result.tests_passed is False
        assert result.follow_up_required is True
        assert "deadline" in result.summary.lower()
        assert any(event.event_type == "task_deadline_exceeded" and
                   event.data["automatic_retry"] is False for event in events)
        await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_dag_deadline_never_escalates_or_runs_another_attempt(tmp_path, monkeypatch):
    async def scenario():
        coordinator, task, entered, cleanup, calls = setup_attempt(tmp_path, monkeypatch, "agent")
        escalation_checks = []
        monkeypatch.setattr(coordinator, "_can_escalate", lambda *a: escalation_checks.append(True) or True)
        with pytest.raises(AgentBlocked) as blocked:
            await asyncio.wait_for(coordinator._execute_plan(TaskPlan(objective="fixture", tasks=[task])), .3)
        assert blocked.value.response.status == TaskStatus.BLOCKED
        assert cleanup == ["agent"]
        assert calls.count("agent") == 1
        assert escalation_checks == []
        assert coordinator.tool_registry.team_mailbox is None
        await coordinator.model_manager.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["dag", "simple"])
def test_caller_cancellation_is_not_converted_to_task_failure(tmp_path, monkeypatch, operation):
    async def scenario():
        coordinator, task, entered, cleanup, calls = setup_attempt(tmp_path, monkeypatch, "agent")
        task = task.model_copy(update={"timeout_seconds": 300})
        operation_coro = (coordinator._attempt_dag_task(task, False) if operation == "dag"
                          else coordinator._execute_planned_simple(task, "fixture", ""))
        running = asyncio.create_task(operation_coro)
        await asyncio.wait_for(entered.wait(), 1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert cleanup == ["agent"]
        assert calls.count("agent") == 1
        await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_backend_timeout_is_not_misreported_as_host_deadline(tmp_path, monkeypatch):
    async def scenario():
        coordinator, task, entered, cleanup, calls = setup_attempt(tmp_path, monkeypatch, None)
        async def provider_timeout(*args, **kwargs):
            raise TimeoutError("fixture provider timeout")
        monkeypatch.setattr(coordinator, "_model_for", provider_timeout)
        result = await coordinator._attempt_dag_task(task, False)
        assert result.status == TaskStatus.FAILED
        assert result.summary == "fixture provider timeout"
        await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_suppressed_deadline_cancellation_cannot_be_reported_as_success(tmp_path, monkeypatch):
    async def scenario():
        coordinator, task, entered, cleanup, calls = setup_attempt(tmp_path, monkeypatch, None)
        async def suppress_cancel():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return AgentResponse(task_id=task.task_id, status=TaskStatus.COMPLETED,
                                     summary="incorrect success", tests_passed=True)
        response = await asyncio.wait_for(coordinator._with_task_deadline(task, suppress_cancel), .3)
        assert response.status == TaskStatus.BLOCKED
        assert response.tests_passed is False
        await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_deadline_during_mutation_preserves_pending_journal_and_blocks_replay(tmp_path, monkeypatch):
    import json
    from local_coder.types import ModelResponse, ToolCall
    from tests.test_harness_recovery import HarnessAgent, Registry, ScriptedModel

    async def scenario():
        coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
        cleanup = []
        class MutatingRegistry(Registry):
            async def execute_tool(self, role, name, arguments):
                self.calls.append((name, arguments))
                (tmp_path / "fixture.txt").write_text("partial fixture effect")
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.append(True)
        registry = MutatingRegistry(coordinator.runtime, tool_names=("write_file",))
        agent = HarnessAgent(ScriptedModel([ModelResponse(tool_calls=[
            ToolCall(name="write_file", arguments={"path": "fixture.txt", "content": "fixture"}),
        ])]), registry)
        async def model_for(*args, **kwargs):
            return object()
        async def repo_map(*args, **kwargs):
            return ""
        monkeypatch.setattr(coordinator, "_model_for", model_for)
        monkeypatch.setattr(coordinator, "_repo_map", repo_map)
        monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent", lambda *a, **kw: agent)
        task = AgentTask(task_id="mutation-deadline", role=AgentRole.SECURITY,
                         objective="fixture only", timeout_seconds=1)
        response = await asyncio.wait_for(coordinator._attempt_dag_task(task, False), 3)
        assert response.status == TaskStatus.BLOCKED
        assert cleanup == [True]
        assert len(registry.calls) == 1
        assert (tmp_path / "fixture.txt").read_text() == "partial fixture effect"
        pending = list(coordinator.runtime.state_dir.glob("run_*_pending.json"))
        assert len(pending) == 1
        record = json.loads(pending[0].read_text())
        assert record["tools"][0]["status"] == "started"
        assert record["tools"][0]["mutation_unknown"] is True
        assert await coordinator.runtime.list_runs() == []
        checkpoint = next(coordinator.runtime.state_dir.glob("checkpoint_*.json"))
        recovered = await coordinator.runtime.recover_from_checkpoint(agent.state.run_id, str(checkpoint))
        assert recovered is not None
        with pytest.raises(ValueError, match="Interrupted tool batch"):
            await recovered.validate_pending_batch()
        await coordinator.runtime.release_run(recovered.run_id)
        await coordinator.model_manager.close_all()
    asyncio.run(scenario())

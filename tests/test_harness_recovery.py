"""The real agent loop must resume at a complete tool batch after restart."""
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from local_coder.agents.base import BaseAgent
from local_coder.core.runtime import AgentRuntime
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.types import (
    AgentMetrics, AgentPhase, AgentRole, AgentState, AgentTask, Message, ModelResponse,
    ProjectConfig, TaskStatus, ToolCall, ToolResult,
)


class ProcessCrash(BaseException):
    """Simulate termination without the normal exception/retry handling."""


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.config = SimpleNamespace(temperature=0.0, max_tokens=100)

    async def generate(self, messages, **kwargs):
        self.calls.append(list(messages))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class Registry:
    def __init__(self, runtime, results=()):
        self.runtime = runtime
        self.results = iter(results)
        self.calls = []

    def get_schemas_for_role(self, role):
        return []

    async def execute_tool(self, role, name, arguments):
        self.calls.append((name, arguments))
        return next(self.results)


class HarnessAgent(BaseAgent):
    role = AgentRole.SECURITY
    system_prompt = "Inspect the supplied code boundary."


def test_restart_after_complete_batch_preserves_history_and_remaining_budgets(tmp_path):
    async def scenario():
        task = AgentTask(role=AgentRole.SECURITY, objective="Inspect", model_name="glm53")
        runtime = AgentRuntime(str(tmp_path))
        first_model = ScriptedModel([
            ModelResponse(content="Inspect and verify", reasoning_content="exact preserved reasoning",
                          prompt_tokens=5, completion_tokens=7, tool_calls=[
                              ToolCall(name="read_file", arguments={"path": "app.py"}),
                              ToolCall(name="run_tests", arguments={"test_path": "tests/test_app.py"}),
                          ]),
            ProcessCrash(),
        ])
        registry = Registry(runtime, [
            ToolResult(success=True, output="code", files_changed=["app.py"]),
            ToolResult(success=False, output="red regression"),
        ])
        agent = HarnessAgent(first_model, registry)
        agent.max_iterations, agent.max_tool_calls, agent.max_test_runs = 3, 3, 2
        with pytest.raises(ProcessCrash):
            await agent.execute(task)
        checkpoints = sorted(runtime.state_dir.glob("checkpoint_*.json"))
        assert checkpoints, "real agent execution never saved a resumable checkpoint"
        checkpoint = max(checkpoints, key=lambda p: json.loads(p.read_text())["iteration"])
        saved = json.loads(checkpoint.read_text())
        assert saved["iteration"] == 1
        assert [m["role"] for m in saved["state"]["messages"]][-3:] == ["assistant", "tool", "tool"]
        assert saved["state"]["tool_calls_used"] == 2

        restarted = AgentRuntime(str(tmp_path))
        recovered = await restarted.recover_from_checkpoint(agent.state.run_id, str(checkpoint))
        assert recovered is not None
        next_model = ScriptedModel([
            ModelResponse(tool_calls=[ToolCall(name="run_tests", arguments={"test_path": "tests/test_app.py"})]),
            ModelResponse(content="Verified boundary.", prompt_tokens=2),
        ])
        next_registry = Registry(restarted, [ToolResult(success=True, output="green regression")])
        resumed = HarnessAgent(next_model, next_registry)
        response = await resumed.resume(recovered)
        assert next_registry.calls == [("run_tests", {"test_path": "tests/test_app.py"})]
        assert next_model.calls[0] == agent.state.messages
        assert next_model.calls[0][-3].reasoning_content == "exact preserved reasoning"
        assert response.status == TaskStatus.COMPLETED
        assert response.tests_passed
        assert response.tests_run == ["tests/test_app.py", "tests/test_app.py"]
        assert response.files_changed == ["app.py"]
        assert response.metrics.tool_calls == 3
        assert response.metrics.model_calls == 3
        assert response.metrics.prompt_tokens == 7
        assert response.metrics.completion_tokens == 7
        assert resumed.state.files_read == {"app.py"}
        assert resumed.state.iteration == 3
        assert resumed.state.test_runs == 2
        assert resumed.state.max_tool_calls == 3
        assert resumed.state.max_test_runs == 2
        final = json.loads((runtime.state_dir / f"run_{agent.state.run_id}_final.json").read_text())
        assert final["phase"] == "done"
        assert final["result"]["summary"] == "Verified boundary."
        assert final["result"]["metrics"]["tool_calls"] == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("limit", ["tool-call", "test-run"])
def test_resume_uses_remaining_budget_and_prior_failed_test(tmp_path, limit):
    async def scenario():
        runtime = AgentRuntime(str(tmp_path))
        agent = HarnessAgent(ScriptedModel([
            ModelResponse(tool_calls=[ToolCall(name="run_tests", arguments={"test_path": "a.py"})]),
            ProcessCrash(),
        ]), Registry(runtime, [ToolResult(success=False, output="red")]))
        agent.max_iterations, agent.max_tool_calls, agent.max_test_runs = 2, (1 if limit == "tool-call" else 10), 1
        with pytest.raises(ProcessCrash):
            await agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Inspect"))
        path = runtime.state_dir / f"checkpoint_{agent.state.run_id}_1.json"
        recovered = await AgentRuntime(str(tmp_path)).recover_from_checkpoint(agent.state.run_id, str(path))
        assert recovered is not None
        registry = Registry(recovered._runtime)
        resumed = HarnessAgent(ScriptedModel([
            ModelResponse(tool_calls=[ToolCall(name="read_file" if limit == "tool-call" else "run_tests")]),
        ]), registry)
        response = await resumed.resume(recovered)
        assert not registry.calls
        assert response.status == TaskStatus.FAILED
        assert not response.tests_passed
        assert f"{limit} limit" in response.issues[0]
        assert resumed.state.iteration == 2
        assert resumed.state.tool_calls_used == 1
    asyncio.run(scenario())


def test_coordinator_resume_selects_latest_boundary_role_and_model(tmp_path, monkeypatch):
    async def scenario():
        coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
        model = ScriptedModel([
            ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "a.py"})]),
            ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "b.py"})]),
            ProcessCrash(),
        ])
        agent = HarnessAgent(model, Registry(coordinator.runtime, [
            ToolResult(success=True, output="a"), ToolResult(success=True, output="b"),
        ]))
        coordinator.model_manager._models = {"qwen38": model}
        with pytest.raises(ProcessCrash):
            await agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Inspect"))
        # A different coordinator models a restarted process and empty run registry.
        coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
        requested = []
        finished_model = ScriptedModel([ModelResponse(content="Resumed.")])
        async def get_model(role, model_name=None):
            requested.append((role, model_name))
            return finished_model
        coordinator.model_manager.get_model = get_model
        monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent",
                            lambda role, model, registry, callback, **kwargs: HarnessAgent(model, registry, callback, **kwargs))
        first = coordinator.runtime.state_dir / f"checkpoint_{agent.state.run_id}_1.json"
        result = await coordinator.resume_checkpoint(str(first))
        assert requested == [(AgentRole.SECURITY, "qwen38")]
        assert result.status == TaskStatus.COMPLETED
        assert result.metrics.tool_calls == 2
        assert finished_model.calls[0][-1].content == "b"
        assert result.task_id == agent.state.task_id
        # Completed runs return the recorded result without another model/tool call.
        assert await coordinator.resume_checkpoint(str(first)) == result
        assert len(requested) == 1
    asyncio.run(scenario())


def test_resume_does_not_reset_exhausted_iteration_limit(tmp_path):
    async def scenario():
        runtime = AgentRuntime(str(tmp_path))
        events = []
        def stop_after_round(event):
            if event.event_type == "checkpoint_saved":
                events.append(event)
                if event.data["path"].endswith("_1.json"):
                    raise ProcessCrash()
        agent = HarnessAgent(ScriptedModel([
            ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "app.py"})]),
        ]), Registry(runtime, [ToolResult(success=True, output="code")]), stop_after_round)
        agent.max_iterations = 1
        with pytest.raises(ProcessCrash):
            await agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Inspect"))
        assert len(events) == 2
        restarted = AgentRuntime(str(tmp_path))
        run = await restarted.recover_from_checkpoint(agent.state.run_id, events[-1].data["path"])
        model = ScriptedModel([])
        resumed = HarnessAgent(model, Registry(restarted))
        response = await resumed.resume(run)
        assert response.status == TaskStatus.FAILED
        assert "maximum tool-calling iterations" in response.issues[0]
        assert response.metrics.model_calls == 1
        assert not model.calls
        assert resumed.state.max_iterations == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["outside", "symlink", "legacy", "unsafe_run_id", "incomplete"])
def test_resume_rejects_invalid_checkpoints_before_model_access(tmp_path, kind):
    async def scenario():
        coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
        path = coordinator.runtime.state_dir / "checkpoint_abcdef123456_1.json"
        if kind == "outside":
            path = tmp_path / "checkpoint_abcdef123456_1.json"
        elif kind == "symlink":
            outside = tmp_path / "outside.json"
            outside.write_text("{}")
            path.symlink_to(outside)
        elif kind == "unsafe_run_id":
            path = coordinator.runtime.state_dir / "checkpoint_unsafe.run_1.json"
        if kind in {"legacy", "incomplete"}:
            task = AgentTask(role=AgentRole.SECURITY, objective="Inspect")
            state = AgentState(task_id=task.task_id, objective=task.objective,
                               phase=AgentPhase.EXECUTING)
            saved_run = await coordinator.runtime.start_run(task, state)
            if kind == "incomplete":
                state.messages = [Message(role="assistant", content="", tool_calls=[ToolCall(name="read_file")])]
            await saved_run.checkpoint_boundary(state, AgentMetrics(), {"boundary_complete": True})
            path = coordinator.runtime.state_dir / f"checkpoint_{saved_run.run_id}_0.json"
            saved = json.loads(path.read_text())
            if kind == "legacy":
                saved.pop("task")
            path.write_text(json.dumps(saved))
            coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
        elif kind != "symlink":
            path.write_text("{}")
        async def forbidden_model(*args, **kwargs):
            raise AssertionError("invalid checkpoint reached model selection")
        coordinator.model_manager.get_model = forbidden_model
        with pytest.raises(ValueError):
            await coordinator.resume_checkpoint(str(path))
    asyncio.run(scenario())


def test_crash_during_batch_never_saves_or_replays_partial_tools(tmp_path):
    async def scenario():
        runtime = AgentRuntime(str(tmp_path))
        class CrashingRegistry(Registry):
            async def execute_tool(self, role, name, arguments):
                if len(self.calls) == 1:
                    raise ProcessCrash()
                return await super().execute_tool(role, name, arguments)
        registry = CrashingRegistry(runtime, [ToolResult(success=True, output="changed", files_changed=["app.py"])])
        agent = HarnessAgent(ScriptedModel([ModelResponse(tool_calls=[
            ToolCall(name="write_file", arguments={"path": "app.py"}),
            ToolCall(name="run_tests"),
        ])]), registry)
        with pytest.raises(ProcessCrash):
            await agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Fix"))
        paths = list(runtime.state_dir.glob("checkpoint_*.json"))
        assert len(paths) == 1
        assert json.loads(paths[0].read_text())["iteration"] == 0
        restarted = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
        async def forbidden(*args, **kwargs):
            raise AssertionError("ambiguous partial batch reached the model")
        restarted.model_manager.get_model = forbidden
        with pytest.raises(ValueError, match="Interrupted tool batch"):
            await restarted.resume_checkpoint(str(paths[0]))
        assert registry.calls == [("write_file", {"path": "app.py"})]
    asyncio.run(scenario())


def test_batch_is_rejected_before_any_tool_if_it_exceeds_remaining_budget(tmp_path):
    async def scenario():
        registry = Registry(AgentRuntime(str(tmp_path)))
        agent = HarnessAgent(ScriptedModel([ModelResponse(tool_calls=[
            ToolCall(name="read_file"), ToolCall(name="read_file"),
        ])]), registry)
        agent.max_tool_calls = 1
        result = await agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Inspect"))
        assert result.status == TaskStatus.FAILED
        assert not registry.calls
        assert result.metrics.tool_calls == 0
    asyncio.run(scenario())


def test_abrupt_process_exit_resumes_without_repeating_completed_side_effects(tmp_path, monkeypatch):
    # No cleanup, exception handler or shared in-memory object can rescue this
    # child. Recovery must use the atomic files written by the real agent loop.
    script = r'''
import asyncio
import os
import sys
from pathlib import Path
from local_coder.agents.base import BaseAgent
from local_coder.core.runtime import AgentRuntime
from local_coder.types import AgentRole, AgentTask, ModelResponse, ToolCall, ToolResult
root = Path(sys.argv[1])
class Model:
    calls = 0
    async def generate(self, messages, **kwargs):
        self.calls += 1
        if self.calls > 1:
            os._exit(73)
        return ModelResponse(tool_calls=[
            ToolCall(name="record", arguments={"value": "one"}),
            ToolCall(name="record", arguments={"value": "two"}),
        ])
class Registry:
    runtime = AgentRuntime(str(root))
    def get_schemas_for_role(self, role):
        return []
    async def execute_tool(self, role, name, arguments):
        with (root / "effects.txt").open("a") as target:
            target.write(arguments["value"] + "\n")
        return ToolResult(success=True, output=arguments["value"], files_changed=["effects.txt"])
class Agent(BaseAgent):
    role = AgentRole.SECURITY
    system_prompt = "Inspect"
asyncio.run(Agent(Model(), Registry()).execute(AgentTask(role=AgentRole.SECURITY, objective="Inspect")))
'''
    crashed = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True,
                             text=True, timeout=15)
    assert crashed.returncode == 73, crashed.stderr
    assert (tmp_path / "effects.txt").read_text() == "one\ntwo\n"
    async def scenario():
        restarted = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
        model = ScriptedModel([ModelResponse(content="Resumed after process exit.")])
        async def get_model(*args):
            return model
        restarted.model_manager.get_model = get_model
        monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent",
                            lambda role, model, registry, callback, **kwargs: HarnessAgent(model, registry, callback, **kwargs))
        async def forbidden_tool(*args):
            raise AssertionError("completed side effect was replayed")
        restarted.tool_registry.execute_tool = forbidden_tool
        checkpoint = next(restarted.runtime.state_dir.glob("checkpoint_*_0.json"))
        result = await restarted.resume_checkpoint(str(checkpoint))
        assert result.status == TaskStatus.COMPLETED
        assert result.metrics.tool_calls == 2
        assert result.files_changed == ["effects.txt"]
        assert [message.content for message in model.calls[0][-2:]] == ["one", "two"]
    asyncio.run(scenario())
    assert (tmp_path / "effects.txt").read_text() == "one\ntwo\n"

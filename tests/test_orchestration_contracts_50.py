"""Host-owned orchestration and goal-evidence regressions."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from local_coder.orchestrator.coordinator import Coordinator
from local_coder.types import AgentResponse, AgentRole, AgentTask, ModelResponse, ProjectConfig, TaskPlan, TaskStatus, ToolCall, ToolResult
from local_coder.memory.goals import GoalLedger


def coordinator(tmp_path):
    return Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))


def test_single_planned_task_retains_contract(tmp_path, monkeypatch):
    instance = coordinator(tmp_path)
    planned = AgentTask(task_id="planned", role=AgentRole.TESTER, objective="Exact objective",
                        files=["test_one.py"], constraints=["local only"],
                        success_criteria=["Check one"], model_name="chosen")
    seen = {}
    async def model(role, name=None):
        seen["routing"] = (role, name)
    class Agent:
        async def execute(self, task):
            seen["task"] = task
            return AgentResponse(task_id=task.task_id, status=TaskStatus.COMPLETED)
    monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent", lambda *a, **k: Agent())
    instance.model_manager.get_model = model
    async def explore(*args):
        return "exploration"
    async def plan(*args):
        return TaskPlan(objective="request", tasks=[planned])
    async def review(*args):
        return "review"
    async def report(*args):
        return "report"
    instance._explore, instance._plan, instance._review, instance._generate_report = explore, plan, review, report
    instance.config.verification.run_tests_after_changes = False
    asyncio.run(instance.run("original request"))
    task = seen["task"]
    for field in ("task_id", "role", "objective", "files", "constraints", "success_criteria", "depends_on", "model_name"):
        assert getattr(task, field) == getattr(planned, field)
    assert seen["routing"] == (AgentRole.TESTER, "chosen")
    assert "exploration" in task.context.previous_findings
    assert planned.context.previous_findings == []


def test_parallel_agents_receive_canonical_host_scope(tmp_path, monkeypatch):
    instance = coordinator(tmp_path)
    instance.config.agentic.max_parallel_agents = 2
    scopes = []
    class Agent:
        async def execute(self, task):
            scopes.append((task.task_id, self.host_write_scope, self.host_parallel_worker))
            return AgentResponse(task_id=task.task_id, status=TaskStatus.COMPLETED)
    monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent", lambda *a, **k: Agent())
    async def model(*args, **kwargs):
        return None
    instance.model_manager.get_model = model
    plan = TaskPlan(objective="split", tasks=[
        AgentTask(task_id="a", role=AgentRole.CODER, objective="one", files=["src/../a.py"]),
        AgentTask(task_id="b", role=AgentRole.CODER, objective="two", files=["b.py"]),
    ])
    result = asyncio.run(instance._execute_plan(plan))
    assert result.status == TaskStatus.COMPLETED
    assert sorted(scopes) == [("a", (str(tmp_path / "a.py"),), True), ("b", (str(tmp_path / "b.py"),), True)]


def test_real_parallel_agents_cannot_write_sibling_scope_or_run_unconfined_command(tmp_path):
    instance = coordinator(tmp_path)
    instance.config.agentic.max_parallel_agents = 2
    observed = []
    tool_outputs = []
    instance.on_event(observed.append)
    class Model:
        config = SimpleNamespace(context_length=131072, max_tokens=1000, temperature=0.0)
        def __init__(self, identity):
            self.identity, self.calls = identity, 0
        async def generate(self, messages, **kwargs):
            self.calls += 1
            if self.calls > 1:
                tool_outputs.extend(message.content for message in messages if message.role == "tool")
                return ModelResponse(content="Observed results")
            if self.identity == "a":
                return ModelResponse(tool_calls=[
                    ToolCall(name="write_file", arguments={"path": "nested/../b.py", "content": "wrong = True\n"}),
                    ToolCall(name="run_command", arguments={"command": "echo local-test"}),
                ])
            return ModelResponse(tool_calls=[ToolCall(name="write_file", arguments={"path": "b.py", "content": "right = True\n"})])
    async def model(role, name=None):
        return Model(name)
    instance.model_manager.get_model = model
    plan = TaskPlan(objective="bounded", tasks=[
        AgentTask(task_id=identity, role=AgentRole.CODER, objective="Local bounded mutation",
                  files=[f"{identity}.py"], model_name=identity)
        for identity in ("a", "b")
    ])
    asyncio.run(instance._execute_plan(plan))
    payloads = json.dumps([event.model_dump(mode="json") for event in observed])
    assert (tmp_path / "b.py").exists(), tool_outputs
    assert (tmp_path / "b.py").read_text() == "right = True\n"
    assert not (tmp_path / "a.py").exists()
    assert any("parallel worker write scope" in output for output in tool_outputs)
    assert any("Unconfined mutation" in output for output in tool_outputs)


def test_direct_tests_publish_observed_verification_result(tmp_path):
    instance = coordinator(tmp_path)
    (tmp_path / "test_fixture.py").write_text("def test_ok(): pass\n")
    observed = []
    instance.on_event(observed.append)
    async def execute(*args):
        return ToolResult(success=False, output="observed failure")
    instance.tool_registry.execute_tool = execute
    response = asyncio.run(instance._run_tests())
    event = next(event for event in observed if event.event_type == "verification_result")
    assert response.tests_passed is False and event.data["passed"] is False
    assert event.data["command"] == "run_tests({})" and event.data["recorded_at"]
    assert event.data["tool"] == "run_tests" and event.data["arguments"] == {}


@pytest.mark.parametrize("passed", [True, False])
def test_real_direct_test_run_event_matches_subprocess_outcome(tmp_path, passed):
    instance = coordinator(tmp_path)
    (tmp_path / "test_real.py").write_text(f"def test_result(): assert {passed!r}\n")
    events = []
    instance.on_event(events.append)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Direct verification must not ask a model")
    instance.model_manager.get_model = forbidden
    response = asyncio.run(instance._run_tests())
    event = next(event for event in events if event.event_type == "verification_result")
    assert event.data["passed"] is passed and response.tests_passed is passed
    assert event.data["command"] == "run_tests({})"


def test_unavailable_tester_does_not_invent_passing_checks(tmp_path, monkeypatch):
    instance = coordinator(tmp_path)
    async def model(*args):
        return None
    instance.model_manager.get_model = model
    monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent", lambda *a, **k: None)
    response = asyncio.run(instance._run_tests())
    assert response.status == TaskStatus.FAILED and not response.tests_passed
    assert response.tests_run == []


def finish(ledger, goal, **kwargs):
    ledger.finish(SimpleNamespace(task_id=goal["id"], status=TaskStatus.COMPLETED,
        tests_run=["pytest fixture"], tests_passed=True, summary="Observed checks"),
        verification_revision=ledger.revision(), **kwargs)


def test_upstream_reverification_invalidates_downstream_without_source_change(tmp_path, monkeypatch):
    ledger = GoalLedger(tmp_path)
    monkeypatch.setattr(ledger, "revision", lambda: "fixed-revision")
    first = ledger.create("First")
    second = ledger.create("Second", dependencies=[first["id"]])
    third = ledger.create("Third", dependencies=[second["id"]])
    for goal in (first, second, third):
        finish(ledger, goal)
    assert not ledger.get(third["id"])["evidence_stale"]
    finish(ledger, first)
    assert ledger.get(second["id"])["evidence_stale"]
    assert ledger.get(third["id"])["evidence_stale"]
    with pytest.raises(ValueError, match="Dependency"):
        ledger.update(third["id"], {"status": "completed"})
    finish(ledger, second)
    assert ledger.get(third["id"])["evidence_stale"]
    finish(ledger, third)
    assert not ledger.get(third["id"])["evidence_stale"]


def test_freeform_criterion_requires_observed_check_qualification(tmp_path, monkeypatch):
    ledger = GoalLedger(tmp_path)
    monkeypatch.setattr(ledger, "revision", lambda: "fixed-revision")
    goal = ledger.create("Feature", criteria="Specific behavior is correct")
    finish(ledger, goal)
    assert ledger.get(goal["id"])["status"] == "verifying"
    assert ledger.get(goal["id"])["acceptance"][0]["status"] == "unresolved"
    finish(ledger, goal, acceptance_checks={"Specific behavior is correct": ["pytest fixture"]})
    assert ledger.get(goal["id"])["status"] == "completed"
    with pytest.raises(ValueError, match="observed"):
        finish(ledger, goal, acceptance_checks={"Specific behavior is correct": ["unobserved assertion"]})


def test_model_summary_without_observed_checks_never_completes_goal(tmp_path, monkeypatch):
    ledger = GoalLedger(tmp_path)
    monkeypatch.setattr(ledger, "revision", lambda: "fixed-revision")
    goal = ledger.create("Verify")
    ledger.finish(SimpleNamespace(task_id=goal["id"], status=TaskStatus.COMPLETED,
        tests_run=[], tests_passed=True, summary="Everything is verified and complete"))
    assert ledger.get(goal["id"])["status"] == "verifying"
    assert ledger.get(goal["id"])["evidence"] is None


def test_cancel_parent_run_stops_owned_leaf_execution(tmp_path, monkeypatch):
    instance = coordinator(tmp_path)
    stopped = []
    async def scenario():
        started = asyncio.Event()
        class Agent:
            async def execute(self, task):
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    stopped.append(True)
        monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent", lambda *a, **k: Agent())
        async def model(*args):
            return None
        instance.model_manager.get_model = model
        delegations = []
        async def work(*args, **kwargs):
            token = instance.tool_registry.task_context.set({"task_id": "parent", "role": "explorer", "files": ["."]})
            try:
                delegations.append(asyncio.create_task(instance._delegate_task({"role": "explorer", "objective": "inspect"})))
                await started.wait()
                await asyncio.sleep(60)
            finally:
                instance.tool_registry.task_context.reset(token)
        instance._run_request = work
        parent = asyncio.create_task(instance.run("Inspect"))
        await asyncio.wait_for(started.wait(), 2)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        await asyncio.gather(*delegations, return_exceptions=True)
        assert stopped == [True] and not instance._owned_children
    asyncio.run(scenario())

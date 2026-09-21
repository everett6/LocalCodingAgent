"""Regression tests for Coordinator orchestration bugs.

These exercise code paths that previously raised NameError (missing
TaskContext import) or could hang forever (unvalidated task DAG).
"""
import asyncio
from types import SimpleNamespace

from local_coder.orchestrator.coordinator import Coordinator
from local_coder.types import (
    AgentRole,
    AgentResponse,
    AgentTask,
    ModelResponse,
    ProjectConfig,
    TaskStatus,
)


class FakeModel:
    """Minimal stand-in for a LocalModel that never issues tool calls."""

    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    async def generate(self, messages, **kwargs):
        self.calls += 1
        return ModelResponse(content=self.content)


def run(coro):
    return asyncio.run(coro)


def make_coordinator(tmp_path) -> Coordinator:
    config = ProjectConfig(project_root=str(tmp_path))
    return Coordinator(config=config, project_root=str(tmp_path))


def test_plan_fallback_on_unparsable_model_output_does_not_crash(tmp_path):
    coordinator = make_coordinator(tmp_path)
    # Planner returns prose instead of the expected JSON plan, forcing the
    # fallback branch that builds a single-task plan with a TaskContext().
    coordinator.model_manager.get_model = lambda role: asyncio.sleep(0, result=FakeModel("Sure thing!"))

    plan = run(coordinator._plan("Add a helper function", "exploration notes"))

    assert len(plan.tasks) == 1
    assert plan.tasks[0].role == AgentRole.CODER


def test_plan_parses_the_schema_the_planner_prompt_declares(tmp_path):
    """The planner's system prompt (agents/planner.py) tells the model to
    return {"tasks": [{"task_id", "objective", "role", "files",
    "constraints", "success_criteria", "depends_on"}, ...]}. Coordinator._plan
    must parse exactly that shape -- a prior mismatch (prompt said
    "subtasks"/"id"/"description", parser read "tasks"/"task_id"/"objective")
    meant a model that followed instructions perfectly still always fell
    through to the single-task fallback."""
    import json as jsonlib

    plan_json = jsonlib.dumps({
        "tasks": [
            {
                "task_id": "1",
                "objective": "Add the calculate_average helper",
                "role": "coder",
                "files": ["utils.py"],
                "constraints": ["keep it pure"],
                "success_criteria": ["utils.py exports calculate_average"],
                "depends_on": [],
            },
            {
                "task_id": "2",
                "objective": "Add a unit test for calculate_average",
                "role": "tester",
                "files": ["tests/test_utils.py"],
                "constraints": [],
                "success_criteria": ["pytest passes"],
                "depends_on": ["1"],
            },
        ],
        "risks": [],
        "notes": "straightforward",
    })
    coordinator = make_coordinator(tmp_path)
    coordinator.model_manager.get_model = lambda role: asyncio.sleep(
        0, result=FakeModel(f"```json\n{plan_json}\n```")
    )

    plan = run(coordinator._plan("Add a helper function", "exploration notes"))

    assert [t.task_id for t in plan.tasks] == ["1", "2"]
    assert plan.tasks[0].objective == "Add the calculate_average helper"
    assert plan.tasks[1].objective == "Add a unit test for calculate_average"
    assert plan.tasks[1].depends_on == ["1"]
    assert plan.tasks[1].role == AgentRole.TESTER


def test_fix_failures_does_not_crash(tmp_path):
    coordinator = make_coordinator(tmp_path)
    coordinator.model_manager.get_model = lambda role: asyncio.sleep(0, result=FakeModel("Applied a fix."))
    test_result = AgentResponse(
        task_id="t",
        status=TaskStatus.FAILED,
        summary="FAILED tests/test_x.py::test_thing - AssertionError",
        tests_run=["tests/test_x.py"],
        tests_passed=False,
        issues=["assertion failed"],
    )

    response = run(coordinator._fix_failures(test_result))

    assert response.summary == "Applied a fix."


def test_execute_plan_fails_fast_on_invalid_dependency(tmp_path):
    """A plan whose depends_on references a nonexistent task must fail
    immediately instead of the DAG loop spinning forever."""
    coordinator = make_coordinator(tmp_path)
    from local_coder.types import TaskPlan

    plan = TaskPlan(
        objective="broken plan",
        tasks=[AgentTask(task_id="only", role=AgentRole.CODER, objective="x", depends_on=["missing"])],
    )

    result = run(asyncio.wait_for(coordinator._execute_plan(plan), timeout=5))

    assert result.status == TaskStatus.FAILED
    assert "missing" in result.summary


def test_agent_events_reach_registered_handlers_without_crashing(tmp_path):
    """create_agent() is handed a callback that BaseAgent invokes as
    callback(event) with a single AgentEvent. Coordinator._emit has a
    different (source, event_type, message, **kwargs) signature, so
    passing it directly used to blow up with a TypeError on the very
    first event any sub-agent emitted. Coordinator._dispatch is the
    correctly-shaped adapter."""
    coordinator = make_coordinator(tmp_path)
    coordinator.model_manager.get_model = lambda role: asyncio.sleep(0, result=FakeModel("done"))
    received = []
    coordinator.on_event(received.append)

    response = run(coordinator._fix_failures(AgentResponse(
        task_id="t", status=TaskStatus.FAILED, tests_passed=False,
    )))

    assert response.summary == "done"
    assert any(e.event_type == "iteration_started" for e in received)


def test_execute_plan_fails_fast_on_cycle(tmp_path):
    coordinator = make_coordinator(tmp_path)
    from local_coder.types import TaskPlan

    plan = TaskPlan(
        objective="cyclic plan",
        tasks=[
            AgentTask(task_id="a", role=AgentRole.CODER, objective="x", depends_on=["b"]),
            AgentTask(task_id="b", role=AgentRole.CODER, objective="y", depends_on=["a"]),
        ],
    )

    result = run(asyncio.wait_for(coordinator._execute_plan(plan), timeout=5))

    assert result.status == TaskStatus.FAILED



def test_execute_plan_runs_independent_tasks_concurrently(tmp_path):
    """Three independent tasks (no depends_on between them) should overlap
    in-flight when agentic.max_parallel_agents > 1, instead of the DAG
    scheduler running them strictly one at a time."""
    from local_coder.types import ProjectConfig, TaskPlan

    config = ProjectConfig(project_root=str(tmp_path))
    config.agentic.max_parallel_agents = 3
    coordinator = Coordinator(config=config, project_root=str(tmp_path))

    in_flight = 0
    max_in_flight = 0

    class SlowFakeModel:
        config = SimpleNamespace(temperature=0.0, max_tokens=100)

        async def generate(self, messages, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return ModelResponse(content="done")

    coordinator.model_manager.get_model = lambda role, model_name=None: asyncio.sleep(
        0, result=SlowFakeModel()
    )

    plan = TaskPlan(
        objective="parallel",
        tasks=[
            AgentTask(task_id="a", role=AgentRole.CODER, objective="x", files=["a.py"]),
            AgentTask(task_id="b", role=AgentRole.CODER, objective="y", files=["b.py"]),
            AgentTask(task_id="c", role=AgentRole.CODER, objective="z", files=["c.py"]),
        ],
    )

    result = run(asyncio.wait_for(coordinator._execute_plan(plan), timeout=5))

    assert result.status == TaskStatus.COMPLETED
    assert max_in_flight >= 2, "independent tasks should have overlapped, not run strictly sequentially"


def test_execute_plan_respects_max_parallel_agents_limit(tmp_path):
    """max_parallel_agents=1 must keep today's behavior: no more than one
    task's model call in flight at a time, even with several ready tasks."""
    from local_coder.types import ProjectConfig, TaskPlan

    config = ProjectConfig(project_root=str(tmp_path))
    config.agentic.max_parallel_agents = 1
    coordinator = Coordinator(config=config, project_root=str(tmp_path))

    in_flight = 0
    max_in_flight = 0

    class SlowFakeModel:
        config = SimpleNamespace(temperature=0.0, max_tokens=100)

        async def generate(self, messages, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return ModelResponse(content="done")

    coordinator.model_manager.get_model = lambda role, model_name=None: asyncio.sleep(
        0, result=SlowFakeModel()
    )

    plan = TaskPlan(
        objective="sequential",
        tasks=[
            AgentTask(task_id="a", role=AgentRole.CODER, objective="x", files=["a.py"]),
            AgentTask(task_id="b", role=AgentRole.CODER, objective="y", files=["b.py"]),
        ],
    )

    result = run(asyncio.wait_for(coordinator._execute_plan(plan), timeout=5))

    assert result.status == TaskStatus.COMPLETED
    assert max_in_flight == 1

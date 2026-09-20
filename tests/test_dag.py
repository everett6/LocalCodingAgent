"""Tests for the task dependency DAG scheduler."""
from local_coder.scheduler.dag import TaskDAG
from local_coder.types import AgentRole, AgentResponse, AgentTask, TaskStatus


def make_task(task_id: str, depends_on: list[str] | None = None) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        role=AgentRole.CODER,
        objective=f"do {task_id}",
        depends_on=depends_on or [],
    )


def make_response(task_id: str, status: TaskStatus = TaskStatus.COMPLETED) -> AgentResponse:
    return AgentResponse(task_id=task_id, status=status, tests_passed=status == TaskStatus.COMPLETED)


def test_get_ready_tasks_respects_dependencies():
    dag = TaskDAG()
    dag.add_tasks([make_task("a"), make_task("b", depends_on=["a"])])

    assert [t.task_id for t in dag.get_ready_tasks()] == ["a"]

    dag.mark_running("a")
    dag.mark_completed("a", make_response("a"))

    assert [t.task_id for t in dag.get_ready_tasks()] == ["b"]


def test_validate_detects_missing_dependency():
    dag = TaskDAG()
    dag.add_tasks([make_task("a", depends_on=["ghost"])])

    errors = dag.validate()

    assert any("ghost" in e for e in errors)


def test_validate_detects_cycle():
    dag = TaskDAG()
    dag.add_tasks([make_task("a", depends_on=["b"]), make_task("b", depends_on=["a"])])

    errors = dag.validate()

    assert any("Cycle" in e for e in errors)


def test_validate_passes_for_well_formed_dag():
    dag = TaskDAG()
    dag.add_tasks([make_task("a"), make_task("b", depends_on=["a"])])

    assert dag.validate() == []


def test_get_pending_task_ids_reports_unscheduled_tasks():
    dag = TaskDAG()
    # "b" depends on a task id that was never added, so it can never
    # become ready: this is exactly the stall scenario the coordinator
    # must detect instead of looping forever.
    dag.add_tasks([make_task("b", depends_on=["missing"])])

    assert dag.get_ready_tasks() == []
    assert dag.get_pending_task_ids() == ["b"]
    assert not dag.is_complete()

"""Plan handoffs retain bounded child observations rather than status IDs only."""
import asyncio

from local_coder.orchestrator.coordinator import Coordinator
from local_coder.types import AgentResponse, AgentRole, AgentTask, ProjectConfig, TaskPlan, TaskStatus


def execute_results(tmp_path, monkeypatch, responses):
    async def scenario():
        coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
        async def attempt(task, escalate):
            return responses[task.task_id]
        monkeypatch.setattr(coordinator, "_attempt_dag_task", attempt)
        plan = TaskPlan(objective="fixture", tasks=[AgentTask(task_id=identity, role=AgentRole.EXPLORER,
            objective="fixture only") for identity in responses])
        result = await coordinator._execute_plan(plan)
        await coordinator.model_manager.close_all()
        return result
    return asyncio.run(scenario())


def test_plan_preserves_child_observations_changes_checks_and_followups(tmp_path, monkeypatch):
    result = execute_results(tmp_path, monkeypatch, {
        "inspect": AgentResponse(task_id="inspect", status=TaskStatus.COMPLETED,
            summary="Found the missing boundary check", files_changed=["a.py", "shared.py"],
            discoveries=["Entry point has two callers"], tests_run=["test_boundary"], tests_passed=True),
        "fix": AgentResponse(task_id="fix", status=TaskStatus.COMPLETED,
            summary="Added a regression", files_changed=["shared.py", "tests/test_boundary.py"],
            tests_run=["test_boundary", "test_negative_control"], tests_passed=True,
            issues=["Manual review remains"], follow_up_required=True),
    })
    assert result.status == TaskStatus.COMPLETED
    assert result.files_changed == ["a.py", "shared.py", "tests/test_boundary.py"]
    assert result.tests_run == ["test_boundary", "test_negative_control"]
    assert result.tests_passed is True
    assert result.discoveries == ["Entry point has two callers"]
    assert any("Manual review remains" in issue for issue in result.issues)
    assert result.follow_up_required is True
    assert "Found the missing boundary check" in result.summary
    assert "Added a regression" in result.summary


def test_plan_keeps_failure_evidence_and_does_not_label_untested_work_verified(tmp_path, monkeypatch):
    result = execute_results(tmp_path, monkeypatch, {
        "read": AgentResponse(task_id="read", status=TaskStatus.COMPLETED, summary="Observed a caller"),
        "failure": AgentResponse(task_id="failure", status=TaskStatus.FAILED,
            summary="Could not reproduce the candidate", tests_run=["test_candidate"],
            tests_passed=False, issues=["Negative control also failed"]),
    })
    assert result.status == TaskStatus.FAILED
    assert result.tests_passed is False
    assert result.tests_run == ["test_candidate"]
    assert any("Negative control also failed" in issue for issue in result.issues)
    assert "Could not reproduce the candidate" in result.summary
    untested = execute_results(tmp_path, monkeypatch, {
        "read": AgentResponse(task_id="read", status=TaskStatus.COMPLETED, summary="Read a function"),
    })
    assert untested.tests_run == []
    assert "no checks recorded" in untested.summary.lower()


def test_plan_result_fields_and_summary_are_bounded_with_explicit_truncation(tmp_path, monkeypatch):
    result = execute_results(tmp_path, monkeypatch, {
        f"task-{number}": AgentResponse(task_id=f"task-{number}", status=TaskStatus.COMPLETED,
            summary="x" * 20000, files_changed=[f"src/file-{index}.py" for index in range(300)],
            tests_run=[f"test-{index}" for index in range(300)],
            issues=["issue" * 2000], discoveries=["detail" * 2000])
        for number in range(12)
    })
    assert len(result.summary) <= 16000
    assert "abbreviated" in result.summary.lower()
    assert len(result.files_changed) <= 200
    assert len(result.tests_run) <= 200
    assert sum(map(len, result.files_changed)) <= 16000
    assert sum(map(len, result.tests_run)) <= 16000
    assert sum(map(len, result.issues)) <= 12000
    assert sum(map(len, result.discoveries)) <= 12000

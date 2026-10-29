import asyncio
import json

from local_coder.core.runtime import (
    AgentRuntime,
    validate_workspace_file_fingerprints,
    workspace_file_fingerprints,
)
from local_coder.intelligence.context_engine import ContextEngine
from local_coder.intelligence.indexer import CodeIndexer
from local_coder.types import AgentPhase, AgentRole, AgentState, AgentTask, Message


def test_workspace_context_fingerprints_detect_changed_and_missing_files(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("before\n")
    fingerprints = workspace_file_fingerprints(str(tmp_path), ["app.py"])

    validate_workspace_file_fingerprints(str(tmp_path), fingerprints)
    source.write_text("after\n")
    try:
        validate_workspace_file_fingerprints(str(tmp_path), fingerprints)
    except ValueError as exc:
        assert "app.py" in str(exc)
        assert "Start a new task" in str(exc)
    else:
        raise AssertionError("changed source should prevent stale-context resume")

    source.unlink()
    try:
        validate_workspace_file_fingerprints(str(tmp_path), fingerprints)
    except ValueError as exc:
        assert "app.py" in str(exc)
    else:
        raise AssertionError("deleted source should prevent stale-context resume")


def test_workspace_context_fingerprints_reject_workspace_escape(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("outside\n")
    try:
        workspace_file_fingerprints(str(tmp_path), [str(outside)])
    except ValueError as exc:
        assert "escapes workspace" in str(exc)
    else:
        raise AssertionError("workspace escape should be rejected")


def test_checkpoint_survives_restart_with_full_task_and_history(tmp_path):
    async def scenario():
        task = AgentTask(role=AgentRole.SECURITY, objective="Inspect boundary", files=["app.py"],
                         model_name="remote", constraints=["read only"])
        state = AgentState(task_id=task.task_id, objective=task.objective,
                           phase=AgentPhase.EXECUTING,
                           messages=[Message(role="user", content="Inspect boundary")])
        runtime = AgentRuntime(str(tmp_path), checkpoint_interval=1)
        run = await runtime.start_run(task, state)
        await run.record_tool_call("read_file", True, {"files_read": ["app.py"], "files_changed": []})
        paths = list(runtime.state_dir.glob("checkpoint_*.json"))
        assert len(paths) == 1
        json.loads(paths[0].read_text())
        restarted = AgentRuntime(str(tmp_path))
        recovered = await restarted.recover_from_checkpoint(run.run_id, str(paths[0]))
        assert recovered is not None
        assert recovered.task == task
        assert recovered.iteration == 1
        assert recovered.files_read == {"app.py"}
        assert recovered.state.messages == state.messages
        await restarted.end_run(run.run_id, recovered.state)
        final = json.loads((runtime.state_dir / f"run_{run.run_id}_final.json").read_text())
        assert final["phase"] == "done"
    asyncio.run(scenario())


def test_ranked_context_includes_paths_and_deduplicates_current_file(tmp_path):
    path = tmp_path / "boundary.py"
    path.write_text("def validate_boundary(value):\n    return value > 0\n")
    async def scenario():
        indexer = CodeIndexer(languages=["python"])
        await indexer.index_file(str(path))
        engine = ContextEngine(indexer)
        context = await engine.generate_context("validate boundary")
        assert str(path) in context
        assert "validate_boundary" in context
        context = await engine.generate_context("validate boundary", current_file=str(path))
        assert context.count("validate_boundary") == 2  # name and qualified name, one file
        assert context.count("File:") == 1
    asyncio.run(scenario())

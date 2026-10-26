import asyncio
import json

from local_coder.core.runtime import AgentRuntime
from local_coder.intelligence.context_engine import ContextEngine
from local_coder.intelligence.indexer import CodeIndexer
from local_coder.types import AgentPhase, AgentRole, AgentState, AgentTask, Message


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

"""Cross-branch guarantees for context compaction and recovered agent rounds."""
import asyncio
import json
from pathlib import Path

import pytest

from local_coder.context.compression import summarize_history
from local_coder.core.runtime import AgentRuntime
from local_coder.types import AgentRole, AgentTask, Message, ModelResponse, ToolCall, ToolResult
from tests.test_harness_recovery import HarnessAgent, ProcessCrash, Registry, ScriptedModel


def test_summary_keeps_latest_multitool_protocol_round_byte_exact():
    details = [{"type": "reasoning.encrypted", "data": "opaque+/= protocol"}]
    latest = Message(role="assistant", content="latest", reasoning_content="exact reasoning",
                     reasoning_details=details, tool_calls=[
                         ToolCall(id="a", name="read_file", arguments={"path": "a.py"}),
                         ToolCall(id="b", name="read_file", arguments={"path": "b.py"}),
                     ])
    tail = [latest, Message(role="tool", content="a" * 500, tool_call_id="a"),
            Message(role="tool", content="b" * 500, tool_call_id="b")]
    messages = [Message(role="system", content="instructions"), Message(role="user", content="task"),
                Message(role="assistant", content="older history" * 100)] + tail
    model = ScriptedModel([ModelResponse(content="Older findings.")])
    compacted, _ = asyncio.run(summarize_history(model, messages, keep_recent_chars=100, max_input_chars=2000))
    assert [m.model_dump_json() for m in compacted[-3:]] == [m.model_dump_json() for m in tail]


def test_summary_checkpoint_restores_counters_and_original_archived_evidence(tmp_path):
    async def scenario():
        runtime = AgentRuntime(str(tmp_path))
        task = AgentTask(role=AgentRole.SECURITY, objective="Inspect")
        turn = lambda name, text: ModelResponse(content=text, tool_calls=[ToolCall(name=name)])
        model = ScriptedModel([
            turn("run_tests", "first evidence " * 100),
            turn("run_tests", "second evidence " * 90),
            ModelResponse(content="Earlier tests passed."),
            turn("run_tests", "Verified summary."),
            ProcessCrash(),
        ])
        agent = HarnessAgent(model, Registry(runtime, [ToolResult(success=True, output="ok")] * 3),
                             context_window_chars=3200, compact_context_chars=2200)
        with pytest.raises(ProcessCrash):
            await agent.execute(task)
        checkpoint = runtime.state_dir / f"checkpoint_{agent.state.run_id}_3.json"
        payload = json.loads(checkpoint.read_text())
        assert payload["notes"]["summary_calls"] == 1
        assert payload["metrics"]["model_calls"] == 4
        reference = payload["notes"]["context_archive_refs"][0]
        assert "first evidence " * 100 in json.loads(Path(reference).read_text())[2]["content"]
        recovered = await AgentRuntime(str(tmp_path)).recover_from_checkpoint(agent.state.run_id, str(checkpoint))
        assert recovered is not None
        resumed = HarnessAgent(ScriptedModel([ModelResponse(content="Updated summary."),
                                             ModelResponse(content="Verified.")]), Registry(recovered._runtime))
        response = await resumed.resume(recovered)
        assert resumed._summary_calls == 2
        assert response.metrics.model_calls == 6
    asyncio.run(scenario())

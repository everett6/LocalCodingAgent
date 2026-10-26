import asyncio
import json

import httpx
import pytest

from local_coder.context.compression import compress_messages
from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.types import Message, ModelConfig, ToolCall


def test_glm_profile_preserves_reasoning_and_requests_deep_investigation():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Inspect", "reasoning_content": "protocol-state",
            "tool_calls": [{"id": "call", "function": {"name": "read_file", "arguments": '{"path":"app.py"}'}}]}, "finish_reason": "tool_calls"}]})
    async def scenario():
        backend = OpenAICompatibleBackend(ModelConfig(name="glm", model_id="glm-5.3", profile="glm53"))
        await backend.close()
        backend.client = httpx.AsyncClient(base_url="https://test.example/v4", transport=httpx.MockTransport(handler))
        response = await backend.generate([Message(role="user", content="Audit")])
        await backend.generate([Message(role="assistant", content=response.content, tool_calls=response.tool_calls,
                                       reasoning_content=response.reasoning_content),
                                Message(role="tool", content="code", tool_call_id="call")])
        assert seen[0]["thinking"] == {"type": "enabled", "clear_thinking": False}
        assert seen[0]["reasoning_effort"] == "max"
        assert seen[1]["messages"][0]["reasoning_content"] == "protocol-state"
        await backend.close()
    asyncio.run(scenario())


def test_qwen_stream_assembles_fragmented_tool_arguments():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "state"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "q1", "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x.py"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}},
        ]
        return httpx.Response(200, content="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")
    async def scenario():
        backend = OpenAICompatibleBackend(ModelConfig(name="q", model_id="qwen3.8-max", profile="qwen38"))
        await backend.close()
        backend.client = httpx.AsyncClient(base_url="https://example.test/v1", transport=httpx.MockTransport(handler))
        response = await backend.generate([Message(role="user", content="Inspect")])
        assert response.tool_calls[0].arguments == {"path": "x.py"}
        assert response.reasoning_content == "state"
        assert response.total_tokens == 12
        assert seen[0]["stream"] is True
        assert seen[0]["enable_thinking"] is True
        await backend.close()
    asyncio.run(scenario())


def test_compaction_never_orphans_tool_results_or_truncates_protocol_reasoning():
    messages = [Message(role="system", content="system"), Message(role="user", content="task"),
        Message(role="assistant", content="old", tool_calls=[ToolCall(id="old", name="read", arguments={})], reasoning_content="old-state"),
        Message(role="tool", content="old" * 100, tool_call_id="old"),
        Message(role="assistant", content="new", tool_calls=[ToolCall(id="new", name="read", arguments={})], reasoning_content="new-state"),
        Message(role="tool", content="new" * 30, tool_call_id="new")]
    compacted = compress_messages(messages, 220)
    calls = {c.id for m in compacted for c in m.tool_calls or []}
    results = {m.tool_call_id for m in compacted if m.role == "tool"}
    assert calls == results
    assert any(m.reasoning_content == "new-state" for m in compacted)


def test_malformed_tool_arguments_never_turn_into_empty_executable_arguments():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "bad", "function": {"name": "write_file", "arguments": "{"}}]}}]})
    async def scenario():
        backend = OpenAICompatibleBackend(ModelConfig(name="x", model_id="x"))
        await backend.close()
        backend.client = httpx.AsyncClient(base_url="https://example.test/v1", transport=httpx.MockTransport(handler))
        with pytest.raises(ValueError, match="tool arguments"):
            await backend.generate([Message(role="user", content="Inspect")])
        await backend.close()
    asyncio.run(scenario())


def test_oversized_reasoning_stops_before_an_oversized_followup_request():
    from tests.test_agents import FakeModel, FakeRegistry, LoopAgent
    from local_coder.types import AgentRole, AgentTask, ModelResponse, ToolResult, TaskStatus
    model = FakeModel([ModelResponse(tool_calls=[ToolCall(id="t", name="read_file", arguments={})],
                                    reasoning_content="state" * 5000), ModelResponse(content="Unexpected")])
    agent = LoopAgent(model, FakeRegistry([ToolResult(success=True, output="code")]),
                      context_window_chars=1500, compact_context_chars=1000)
    result = asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Inspect")))
    assert result.status == TaskStatus.FAILED
    assert len(model.calls) == 1
    assert "context" in result.summary.lower()

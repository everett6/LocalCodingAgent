import asyncio
import json

import httpx
import pytest

from local_coder.models.manager import ModelManager
from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.orchestrator.config_loader import load_config
from local_coder.types import Message, ModelConfig, ProjectConfig, ToolCall


def test_key_is_redacted_and_compatible_client_authenticates(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-private-key")
    config = ModelConfig(name="cloud", model_id="model", backend="openai_compatible",
                         base_url="https://api.example.test/v1", api_key_env="TEST_PROVIDER_KEY")
    backend = OpenAICompatibleBackend(config)
    assert backend.client.headers.get("Authorization") == "Bearer test-private-key"
    direct = ModelConfig(name="x", model_id="x", api_key="another-private-key")
    assert "another-private-key" not in repr(direct)
    assert "another-private-key" not in direct.model_dump_json()
    asyncio.run(backend.close())


def test_tool_arguments_use_valid_json():
    backend = OpenAICompatibleBackend(ModelConfig(name="x", model_id="x"))
    result = backend._build_messages([Message(role="assistant", content="", tool_calls=[
        ToolCall(id="t", name="search", arguments={"path": "it's.py", "all": True})])])
    assert json.loads(result[0]["tool_calls"][0]["function"]["arguments"])["all"] is True
    asyncio.run(backend.close())


def test_anthropic_roundtrip_tool_results_and_usage():
    from local_coder.models.anthropic import AnthropicBackend
    seen = []
    def handler(request):
        seen.append((request, json.loads(request.content)))
        return httpx.Response(200, json={"model": "claude-test", "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "Inspecting"},
                        {"type": "tool_use", "id": "next", "name": "read_file", "input": {"path": "app.py"}}],
            "usage": {"input_tokens": 12, "output_tokens": 8}})
    async def scenario():
        config = ModelConfig(name="claude", backend="anthropic", model_id="claude-test",
                             base_url="https://api.anthropic.com/v1", api_key="test-key")
        manager = ModelManager(ProjectConfig(models={"default": config}))
        backend = await manager.get_model("coder")
        assert isinstance(backend, AnthropicBackend)
        await backend.client.aclose()
        backend.client = httpx.AsyncClient(base_url=config.base_url, headers=backend.headers,
                                          transport=httpx.MockTransport(handler))
        response = await backend.generate([
            Message(role="system", content="Be careful"), Message(role="user", content="Inspect"),
            Message(role="assistant", content="", tool_calls=[ToolCall(id="a", name="read_file", arguments={})]),
            Message(role="tool", content="contents", tool_call_id="a")],
            tools=[{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object"}}}])
        request, payload = seen[0]
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert payload["system"] == "Be careful"
        assert payload["messages"][-1]["content"][0]["tool_use_id"] == "a"
        assert payload["tools"][0]["input_schema"] == {"type": "object"}
        assert response.tool_calls[0].arguments == {"path": "app.py"}
        assert response.total_tokens == 20
        await manager.close_all()
    asyncio.run(scenario())


def test_anthropic_stream_and_error_does_not_expose_key():
    from local_coder.models.anthropic import AnthropicBackend
    async def scenario():
        config = ModelConfig(name="c", backend="anthropic", model_id="claude-test", api_key="private")
        backend = AnthropicBackend(config)
        await backend.close()
        backend.client = httpx.AsyncClient(base_url="https://api.anthropic.com/v1", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content='data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n\n')))
        assert [x async for x in backend.stream([Message(role="user", content="Hi")])] == ["hello"]
        await backend.close()
    asyncio.run(scenario())


def test_load_provider_and_mode_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('mode: cybersecurity\nsafety_tier: tier_1_paranoia\nmodels:\n  default:\n    backend: anthropic\n    model_id: claude-test\n    base_url: https://api.anthropic.com/v1\n    api_key_env: ANTHROPIC_API_KEY\n')
    config = load_config(str(path), str(tmp_path))
    assert config.mode.value == "cybersecurity"
    assert config.safety_tier.value == "tier_1_paranoia"
    assert config.models["default"].api_key_env == "ANTHROPIC_API_KEY"
    assert config.models["default"].backend.value == "anthropic"

"""Offline regressions for provider completion and helper-cache boundaries."""
import asyncio
import json

import httpx
import pytest

from local_coder.models.cache import prompt_cache_key
from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.types import Message, ModelBackend, ModelConfig


@pytest.mark.parametrize("ending", ["", "data: [DONE]\n\n"])
def test_truncated_tool_stream_cannot_be_returned_as_complete(ending):
    asyncio.run(_truncated_tool_stream(ending))


async def _truncated_tool_stream(ending):
    chunk = {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "call_1", "function": {
            "name": "edit_file", "arguments": '{"path":"a.py"}'
        }
    }]}}]}
    backend = OpenAICompatibleBackend(ModelConfig(
        name="q", model_id="q", backend=ModelBackend.OPENAI_COMPAT,
        profile="qwen38",
    ))
    await backend.client.aclose()
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\n" + ending)
    ), base_url="http://localhost/v1")
    try:
        with pytest.raises(RuntimeError, match="incomplete"):
            await backend.generate([Message(role="user", content="fix it")])
    finally:
        await backend.close()


@pytest.mark.parametrize("changes", [
    {"base_url": "http://localhost:9999/v1"},
    {"temperature": 0.8},
    {"max_tokens": 8},
    {"context_length": 1024},
    {"profile": "qwen38"},
])
def test_helper_cache_separates_inference_configuration(changes):
    config = ModelConfig(name="draft", model_id="same-model")
    messages = [Message(role="user", content="predict next action")]
    assert prompt_cache_key(config, messages) != prompt_cache_key(
        config.model_copy(update=changes), messages
    )

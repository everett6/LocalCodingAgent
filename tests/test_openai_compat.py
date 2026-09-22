"""Regression test for the OpenAI-compatible backend's URL construction.

config.base_url is documented (and used throughout config/config.yaml) as
already including the server's own "/v1" prefix -- the same convention the
OpenAI SDK itself uses (e.g. base_url="https://api.openai.com/v1"). httpx's
AsyncClient(base_url=...) joins a leading-slash request path onto that
existing path rather than replacing it, so a hardcoded "/v1/chat/completions"
request path turned every real request into ".../v1/v1/chat/completions" --
a 404 against any real server. No test exercised this before because no
local model server was ever reachable in earlier test runs; running this
project against a real llama.cpp server for the first time is what
surfaced it.
"""
import asyncio
import json

import httpx

from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.types import Message, ModelBackend, ModelConfig


def run(coro):
    return asyncio.run(coro)


def make_backend(handler) -> OpenAICompatibleBackend:
    config = ModelConfig(
        name="test",
        backend=ModelBackend.OPENAI_COMPAT,
        model_id="test-model",
        base_url="http://localhost:8090/v1",
    )
    backend = OpenAICompatibleBackend(config)
    backend.client = httpx.AsyncClient(
        base_url=config.base_url, transport=httpx.MockTransport(handler),
    )
    return backend


def test_generate_requests_chat_completions_without_doubling_v1():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": "test-model",
        })

    backend = make_backend(handler)

    response = run(backend.generate([Message(role="user", content="hello")]))

    assert seen_urls == ["http://localhost:8090/v1/chat/completions"]
    assert response.content == "hi"


def test_is_available_requests_models_without_doubling_v1():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    backend = make_backend(handler)

    assert run(backend.is_available()) is True
    assert seen_urls == ["http://localhost:8090/v1/models"]


def test_get_model_info_requests_specific_model_without_doubling_v1():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"owned_by": "local", "created": 0})

    backend = make_backend(handler)

    info = run(backend.get_model_info())

    assert seen_urls == ["http://localhost:8090/v1/models/test-model"]
    assert info["owner"] == "local"


def test_stream_requests_chat_completions_without_doubling_v1():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        body = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    backend = make_backend(handler)

    async def collect():
        return [chunk async for chunk in backend.stream([Message(role="user", content="hello")])]

    chunks = run(collect())

    assert seen_urls == ["http://localhost:8090/v1/chat/completions"]
    assert chunks == ["hi"]


def test_save_slot_posts_to_server_root_not_under_v1():
    """/slots lives at the llama-server root (http://host:port/slots/...),
    unlike every other endpoint here -- it must NOT be requested under the
    base_url's "/v1" path. Contract (action, body, response shape) verified
    against a real llama-server: POST /slots/{id}?action=save
    {"filename": "..."} -> 200 with save stats on success."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.read()))
        return httpx.Response(200, json={"id_slot": 0, "filename": "x.bin", "n_saved": 5})

    backend = make_backend(handler)

    ok = run(backend.save_slot("x.bin"))

    assert ok is True
    [(url, body)] = seen
    assert url == "http://localhost:8090/slots/0?action=save"
    assert json.loads(body) == {"filename": "x.bin"}


def test_restore_slot_posts_to_server_root_not_under_v1():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"id_slot": 0, "filename": "x.bin", "n_restored": 5})

    backend = make_backend(handler)

    ok = run(backend.restore_slot("x.bin", slot_id=2))

    assert ok is True
    assert seen == ["http://localhost:8090/slots/2?action=restore"]


def test_slot_save_and_restore_are_best_effort_on_failure():
    """A 400 (e.g. the server wasn't started with --slot-save-path, or the
    saved file doesn't exist) must come back as False, never raise -- this
    is a latency optimization, not something a run should ever fail over."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "slot save is disabled"})

    backend = make_backend(handler)

    assert run(backend.save_slot("x.bin")) is False
    assert run(backend.restore_slot("x.bin")) is False


def test_slot_save_and_restore_survive_connection_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    backend = make_backend(handler)

    assert run(backend.save_slot("x.bin")) is False
    assert run(backend.restore_slot("x.bin")) is False

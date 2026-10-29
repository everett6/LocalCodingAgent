"""Count actual streamed reads; buffered MockTransport bodies are insufficient."""
import asyncio
import gzip
import json
import zlib

import httpx
import pytest

from local_coder.core.budgets import BudgetLedger, BudgetedModel
from local_coder.models.anthropic import AnthropicBackend
from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.providers import http_response
from local_coder.types import ModelConfig


class CountedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, block_after=False):
        self.chunks = chunks
        self.reads = 0
        self.bytes_read = 0
        self.closed = False
        self.block_after = block_after
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            self.bytes_read += len(chunk)
            yield chunk
        if self.block_after:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


async def backend_for(kind, stream, *, status=200, headers=None, guard=None):
    config = ModelConfig(name="fixture", model_id="fixture", api_key="fixture-only-key",
                         backend="anthropic" if kind == "anthropic" else "openai_compatible",
                         base_url="https://fixture.invalid/v1", profile="qwen38" if kind == "qwen" else "generic")
    backend = AnthropicBackend(config) if kind == "anthropic" else OpenAICompatibleBackend(config, quota_guard=guard)
    await backend.close()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, stream=stream)

    backend.client = httpx.AsyncClient(base_url=config.base_url, transport=httpx.MockTransport(handler))
    return backend, requests


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_completion_read_stops_at_default_limit_without_buffering_the_rest(kind):
    async def go():
        stream = CountedStream((b"x" * 65536 for _ in range(1000)))
        backend, requests = await backend_for(kind, stream)
        ledger = BudgetLedger()
        try:
            with pytest.raises(RuntimeError, match="byte limit"):
                await BudgetedModel(backend, ledger).generate([], max_tokens=128)
            assert stream.reads == http_response.MAX_COMPLETION_BYTES // 65536 + 1
            assert stream.closed and len(requests) == 1
            assert requests[0].headers["accept-encoding"] == "identity"
            entry = ledger.status()["entries"][0]
            assert entry["status"] == "failed" and entry["tokens"] == entry["tokens_reserved"]
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "qwen", "anthropic"])
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_compressed_bomb_is_rejected_before_any_body_read(kind, encoding):
    async def go():
        packed = gzip.compress(b"x" * 2_000_000) if encoding == "gzip" else zlib.compress(b"x" * 2_000_000)
        stream = CountedStream([packed])
        backend, requests = await backend_for(kind, stream, headers={"Content-Encoding": encoding})
        try:
            with pytest.raises(RuntimeError, match="content encoding"):
                await backend.generate([])
            assert stream.reads == 0 and stream.closed and len(requests) == 1
        finally:
            await backend.close()

    asyncio.run(go())


def test_declared_oversize_is_rejected_before_consumption():
    async def go():
        stream = CountedStream([b"not read"])
        backend, _ = await backend_for("openai", stream, headers={"Content-Length": str(http_response.MAX_COMPLETION_BYTES + 1)})
        try:
            with pytest.raises(RuntimeError, match="byte limit"):
                await backend.generate([])
            assert stream.reads == 0 and stream.closed
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
def test_sse_multiline_data_comments_bom_and_split_utf8(ending):
    body = b"\xef\xbb\xbf: comment" + ending + b"event: custom" + ending + b"id: ignored" + ending
    body += b'data: {"value":' + ending + 'data:"café 🤖"}'.encode() + ending + ending
    body += b"data: unfinished"  # no blank line: discarded by the SSE framing contract

    async def go():
        stream = CountedStream((body[index:index + 1] for index in range(len(body))))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as client:
            async with client.stream("GET", "https://fixture.invalid/") as response:
                events = [event async for event in http_response.sse_data(response)]
        assert [json.loads(event) for event in events] == [{"value": "café 🤖"}]
        assert stream.closed

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_multiline_sse_dispatches_complete_json_in_both_adapters(kind):
    data = ({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "complete"}}
            if kind == "anthropic" else {"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]})
    body = "\r\n".join("data: " + line for line in json.dumps(data, indent=2).splitlines()) + "\r\n\r\n"
    body += 'data:{"type":"message_stop"}\r\n\r\n' if kind == "anthropic" else "data:[DONE]\r\n\r\n"

    async def go():
        stream = CountedStream([body.encode()])
        backend, requests = await backend_for(kind, stream)
        try:
            assert [part async for part in backend.stream([])] == ["complete"]
            assert stream.closed and len(requests) == 1
        finally:
            await backend.close()

    asyncio.run(go())


def test_multiline_qwen_stream_preserves_tool_arguments_reasoning_and_usage():
    frames = [
        {"choices": [{"delta": {"reasoning_content": "protocol state", "reasoning_details": [{"type": "fixture"}],
             "tool_calls": [{"index": 0, "id": "call", "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"fixture.py"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    body = "".join("\n".join("data:" + line for line in json.dumps(frame, indent=2).splitlines()) + "\n\n" for frame in frames)
    body += "data:[DONE]\n\n"

    async def go():
        stream = CountedStream((body.encode()[offset:offset + 7] for offset in range(0, len(body.encode()), 7)))
        backend, _ = await backend_for("qwen", stream)
        try:
            result = await backend.generate([])
            assert result.tool_calls[0].arguments == {"path": "fixture.py"}
            assert result.reasoning_content == "protocol state" and result.reasoning_details == [{"type": "fixture"}]
            assert result.total_tokens == 15 and stream.closed
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["qwen", "anthropic"])
def test_oversized_sse_event_fails_before_the_rest_of_the_stream(kind, monkeypatch):
    monkeypatch.setattr(http_response, "MAX_EVENT_BYTES", 128)

    async def go():
        stream = CountedStream([b'data:{"value":"', *([b"x" * 64] * 1000)])
        backend, _ = await backend_for(kind, stream)
        try:
            with pytest.raises(RuntimeError, match="event exceeded"):
                if kind == "qwen":
                    await backend.generate([])
                else:
                    async for _ in backend.stream([]):
                        pass
            assert stream.reads <= 3 and stream.closed
        finally:
            await backend.close()

    asyncio.run(go())


def test_total_stream_limit_bounds_small_comments_and_events(monkeypatch):
    monkeypatch.setattr(http_response, "MAX_STREAM_BYTES", 128)

    async def go():
        stream = CountedStream([b": keepalive\n\n"] * 1000)
        backend, _ = await backend_for("qwen", stream)
        try:
            with pytest.raises(RuntimeError, match="response exceeded"):
                await backend.generate([])
            assert stream.bytes_read <= 128 + len(b": keepalive\n\n") and stream.closed
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "qwen", "anthropic"])
def test_cancellation_closes_response_and_preserves_failed_reservation(kind):
    async def go():
        first = b'data:{"choices":[{"delta":{"content":"partial"}}]}\n\n' if kind == "qwen" else b'{"partial":'
        stream = CountedStream([first], block_after=True)
        backend, requests = await backend_for(kind, stream)
        ledger = BudgetLedger()
        try:
            task = asyncio.create_task(BudgetedModel(backend, ledger).generate([], max_tokens=128))
            await asyncio.wait_for(stream.waiting.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.closed and len(requests) == 1
            entry = ledger.status()["entries"][0]
            assert entry["status"] == "failed" and entry["tokens"] == entry["tokens_reserved"]
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("method", ["generate", "stream"])
def test_quota_error_body_is_bounded_before_observation(method, monkeypatch):
    monkeypatch.setattr("local_coder.models.openai_compat.MAX_ERROR_BYTES", 128)
    observed = []

    class Guard:
        def observe_http_error(self, *args):
            observed.append(args)

    async def go():
        stream = CountedStream([b"x" * 64] * 1000)
        backend, requests = await backend_for("openai", stream, status=429, guard=Guard())
        try:
            with pytest.raises(RuntimeError, match="byte limit"):
                if method == "generate":
                    await backend.generate([])
                else:
                    async for _ in backend.stream([]):
                        pass
            assert stream.reads == 3 and not observed
            assert stream.closed and len(requests) == 1
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_unused_http_error_body_is_not_consumed_or_echoed(kind):
    async def go():
        stream = CountedStream([b"private-fixture-error" * 100000])
        backend, requests = await backend_for(kind, stream, status=401)
        try:
            with pytest.raises(RuntimeError) as caught:
                await backend.generate([])
            assert "private-fixture" not in str(caught.value)
            assert stream.reads == 0 and stream.closed and len(requests) == 1
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_availability_does_not_buffer_unused_model_list(kind):
    async def go():
        stream = CountedStream([b"x" * 65536] * 1000)
        backend, requests = await backend_for(kind, stream)
        try:
            assert await backend.is_available()
            assert stream.reads == 0 and stream.closed and len(requests) == 1
        finally:
            await backend.close()

    asyncio.run(go())


def test_optional_model_metadata_falls_back_after_bounded_read_failure():
    async def go():
        stream = CountedStream([b"x" * 65536] * 1000)
        backend, requests = await backend_for("openai", stream)
        try:
            info = await backend.get_model_info()
            assert info["model_id"] == "fixture"
            assert stream.reads == http_response.MAX_METADATA_BYTES // 65536 + 1
            assert stream.closed and len(requests) == 1
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("operation", ["save_slot", "restore_slot"])
def test_slot_response_bodies_are_unused_and_never_buffered(operation):
    async def go():
        stream = CountedStream([b"x" * 65536] * 1000)
        backend, requests = await backend_for("openai", stream)
        try:
            assert await getattr(backend, operation)("fixture.bin")
            assert stream.reads == 0 and stream.closed and len(requests) == 1
            assert requests[0].headers["accept-encoding"] == "identity"
        finally:
            await backend.close()

    asyncio.run(go())


def test_unterminated_sse_tool_call_is_never_dispatched():
    data = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "partial", "function": {
        "name": "write_file", "arguments": '{"path":"fixture.txt"}'}}]}, "finish_reason": "tool_calls"}]}

    async def go():
        stream = CountedStream([("data:" + json.dumps(data)).encode()])
        backend, requests = await backend_for("qwen", stream)
        try:
            with pytest.raises(RuntimeError, match="incomplete"):
                await backend.generate([])
            assert stream.closed and len(requests) == 1
        finally:
            await backend.close()

    asyncio.run(go())


def test_invalid_utf8_event_fails_without_exposing_payload():
    async def go():
        stream = CountedStream([b'data:{"value":"private-fixture\xff"}\n\n'])
        backend, _ = await backend_for("qwen", stream)
        try:
            with pytest.raises(RuntimeError, match="UTF-8") as caught:
                await backend.generate([])
            assert "private-fixture" not in str(caught.value) and stream.closed
        finally:
            await backend.close()

    asyncio.run(go())


def test_many_argument_fragments_preserve_the_complete_tool_call():
    argument = json.dumps({"path": "fixture.py", "note": "x" * 1000})
    frames = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call", "function": {"name": "read_file"}}]}}]}]
    frames.extend({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": char}}]}}]} for char in argument)
    frames.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    chunks = [("data:" + json.dumps(frame) + "\n\n").encode() for frame in frames]

    async def go():
        stream = CountedStream(chunks)
        backend, _ = await backend_for("qwen", stream)
        try:
            result = await backend.generate([])
            assert result.tool_calls[0].id == "call" and result.tool_calls[0].name == "read_file"
            assert result.tool_calls[0].arguments == json.loads(argument)
            assert stream.closed
        finally:
            await backend.close()

    asyncio.run(go())

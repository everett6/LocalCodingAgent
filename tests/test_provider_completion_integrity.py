"""Provider completion integrity using synthetic HTTP responses only."""
import asyncio
import json

import httpx
import pytest

from local_coder.core.budgets import BudgetExceeded, BudgetLedger, BudgetedModel, request_telemetry
from local_coder.context.telemetry import ContextTelemetry
from local_coder.models.anthropic import AnthropicBackend
from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.types import Message, ModelConfig


def backend_for(kind, handler):
    config = ModelConfig(name="fixture", model_id="fixture", backend="anthropic" if kind == "anthropic" else "openai_compatible",
                         base_url="https://fixture.invalid/v1", api_key="fixture-only-key",
                         profile="qwen38" if kind == "qwen" else "generic",
                         input_cost_per_million=1, output_cost_per_million=1, max_tokens=128)
    backend = AnthropicBackend(config) if kind == "anthropic" else OpenAICompatibleBackend(config)

    async def replace():
        await backend.close()
        backend.client = httpx.AsyncClient(base_url=config.base_url, transport=httpx.MockTransport(handler))

    asyncio.run(replace())
    return backend


@pytest.mark.parametrize("kind", ["openai", "qwen", "anthropic"])
def test_truncated_completion_keeps_usage_but_never_exposes_tool_calls(kind):
    if kind == "anthropic":
        data = {"content": [{"type": "tool_use", "id": "partial", "name": "write_file", "input": {"path": "fixture.txt"}}],
                "stop_reason": "max_tokens", "usage": {"input_tokens": 10, "output_tokens": 5}}
    else:
        call = {"id": "partial", "function": {"name": "write_file", "arguments": '{"path":"fixture.txt"}'}}
        choice = {"finish_reason": "length"}
        if kind == "qwen":
            choice["delta"] = {"tool_calls": [{"index": 0, **call}]}
        else:
            choice["message"] = {"tool_calls": [call]}
        data = {"choices": [choice], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    def handler(request):
        if kind == "qwen":
            return httpx.Response(200, text="data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json=data)

    backend = backend_for(kind, handler)

    async def go():
        try:
            result = await backend.generate([Message(role="user", content="fixture")])
            assert not result.tool_calls
            assert result.finish_reason in {"length", "max_tokens"}
            assert result.prompt_tokens == 10 and result.completion_tokens == 5
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_text_stream_eof_without_terminal_event_is_failure(kind):
    data = ({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}
            if kind == "anthropic" else {"choices": [{"delta": {"content": "partial"}}]})
    backend = backend_for(kind, lambda request: httpx.Response(200, text="data: " + json.dumps(data) + "\n\n"))

    async def go():
        try:
            with pytest.raises(RuntimeError, match="incomplete"):
                async for _ in backend.stream([]):
                    pass
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_sse_optional_space_and_terminal_event_are_supported(kind):
    if kind == "anthropic":
        frames = [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "complete"}}, {"type": "message_stop"}]
    else:
        frames = [{"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]}]
    body = "".join("data:" + json.dumps(data) + "\n\n" for data in frames)
    backend = backend_for(kind, lambda request: httpx.Response(200, text=body))

    async def go():
        try:
            assert [text async for text in backend.stream([])] == ["complete"]
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_partial_or_coerced_usage_cannot_release_conservative_budget(kind):
    usage = {"input_tokens": 1} if kind == "anthropic" else {"prompt_tokens": "1", "completion_tokens": 1}
    data = ({"content": [{"type": "text", "text": "complete"}], "stop_reason": "end_turn", "usage": usage}
            if kind == "anthropic" else {"choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}], "usage": usage})
    backend = backend_for(kind, lambda request: httpx.Response(200, json=data))
    ledger = BudgetLedger()

    async def go():
        try:
            await BudgetedModel(backend, ledger).generate([Message(role="user", content="fixture")], max_tokens=128)
            entry = ledger.status()["entries"][0]
            assert entry["usage"] == "conservative_reservation"
            assert entry["tokens"] == entry["tokens_reserved"]
        finally:
            await backend.close()

    asyncio.run(go())


def test_cached_anthropic_counts_are_complete_and_unknown_cost_blocks_next_capped_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(True)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "complete"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30}})

    backend = backend_for("anthropic", handler)
    ledger = BudgetLedger(max_cost_usd=1)
    telemetry = ContextTelemetry(tmp_path)

    async def go():
        try:
            with request_telemetry(telemetry, "fixture"):
                result = await BudgetedModel(backend, ledger).generate([], max_tokens=128)
            assert result.prompt_tokens == 60 and result.total_tokens == 65
            assert result.usage_details == {"uncached_input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30}
            assert not result.usage_cost_known
            status = ledger.status()
            entry = status["entries"][0]
            assert not status["cost_known"] and not entry["cost_known"]
            assert entry["cost_usd"] > 0
            assert entry["tokens"] == 65 and entry["tokens_reserved"] > 65
            assert ledger.accounting_status()["entries"][0]["cost_known"] is False
            assert entry["provider_actual"]["usage_details"] == result.usage_details
            row = telemetry.inspect()["requests"][0]
            assert row["usage"]["prompt_tokens"] == 60 and row["cost_usd"] is None
            with pytest.raises(BudgetExceeded, match="unknown cost"):
                await BudgetedModel(backend, ledger).generate([], max_tokens=128)
            assert len(calls) == 1 and ledger.status()["requests"] == 1
        finally:
            await backend.close()

    asyncio.run(go())


def test_known_free_pricing_remains_zero_with_cache_usage(tmp_path):
    backend = backend_for("anthropic", lambda request: httpx.Response(200, json={
        "content": [{"type": "text", "text": "complete"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 10}}))
    backend.config.input_cost_per_million = backend.config.output_cost_per_million = 0
    ledger = BudgetLedger(max_cost_usd=0)
    telemetry = ContextTelemetry(tmp_path)

    async def go():
        try:
            with request_telemetry(telemetry, "fixture"):
                await BudgetedModel(backend, ledger).generate([], max_tokens=128)
                await BudgetedModel(backend, ledger).generate([], max_tokens=128)
            assert ledger.status()["cost_known"] and ledger.status()["cost_usd"] == 0
            assert all(row["cost_usd"] == 0 for row in telemetry.inspect()["requests"])
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("changed", [None, float("nan"), 1000])
def test_request_prices_are_bound_to_admission_even_if_config_changes(changed, tmp_path):
    from types import SimpleNamespace

    config = SimpleNamespace(model_id="fixture", max_tokens=128, input_cost_per_million=1,
                             output_cost_per_million=2, request_cost_usd=.01)

    class MutatingModel:
        async def generate(self, *args, **kwargs):
            config.input_cost_per_million = config.output_cost_per_million = config.request_cost_usd = changed
            from local_coder.types import ModelResponse
            return ModelResponse(prompt_tokens=10, completion_tokens=5)

    model = MutatingModel()
    model.config = config
    ledger = BudgetLedger(max_cost_usd=1)
    telemetry = ContextTelemetry(tmp_path)

    async def go():
        with request_telemetry(telemetry, "fixture"):
            await BudgetedModel(model, ledger).generate([], max_tokens=128)

    asyncio.run(go())
    status = ledger.status()
    assert status["cost_known"] and status["cost_usd"] == pytest.approx(.01002)
    assert telemetry.inspect()["requests"][0]["cost_usd"] == pytest.approx(.01002)
    json.dumps(status, allow_nan=False)


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_explicit_complete_zero_usage_can_settle_without_erasing_request_fee(kind):
    usage = {"input_tokens": 0, "output_tokens": 0} if kind == "anthropic" else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    data = ({"content": [{"type": "text", "text": "complete"}], "stop_reason": "end_turn", "usage": usage}
            if kind == "anthropic" else {"choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}], "usage": usage})
    backend = backend_for(kind, lambda request: httpx.Response(200, json=data))
    backend.config.request_cost_usd = .01
    ledger = BudgetLedger(max_cost_usd=1)

    async def go():
        try:
            result = await BudgetedModel(backend, ledger).generate([], max_tokens=128)
            assert result.usage_reported
            entry = ledger.status()["entries"][0]
            assert entry["tokens"] == 0 and entry["cost_usd"] == .01
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("details", [
    {"private_payload": 1}, {"cache_read_input_tokens": "1"},
    {"cache_read_input_tokens": True}, {"cache_read_input_tokens": -1},
    {"cache_read_input_tokens": 1_000_000_000_001},
])
def test_response_usage_details_are_bounded_numeric_categories(details):
    from pydantic import ValidationError
    from local_coder.types import ModelResponse

    with pytest.raises(ValidationError):
        ModelResponse(usage_details=details)


def test_unrequested_stream_choice_cannot_supply_tool_calls():
    data = {"choices": [{"index": 1, "delta": {"tool_calls": [{"index": 0, "id": "other-choice",
             "function": {"name": "write_file", "arguments": '{"path":"fixture.txt"}'}}]}, "finish_reason": "tool_calls"}]}
    backend = backend_for("qwen", lambda request: httpx.Response(200, text="data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n"))

    async def go():
        try:
            with pytest.raises(RuntimeError, match="choice"):
                await backend.generate([])
        finally:
            await backend.close()

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_stream_truncation_and_malformed_events_remain_failed_reservations(kind):
    for malformed in (False, True):
        data = ({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}
                if kind == "anthropic" else {"choices": [{"finish_reason": "length", "delta": {"content": "partial"}}]})
        body = "data: {private-fixture-invalid\n\n" if malformed else "data: " + json.dumps(data) + "\n\n"
        backend = backend_for(kind, lambda request: httpx.Response(200, text=body))
        ledger = BudgetLedger()

        async def go():
            try:
                with pytest.raises(RuntimeError) as caught:
                    async for _ in BudgetedModel(backend, ledger).stream([], max_tokens=128):
                        pass
                assert "private-fixture" not in str(caught.value)
                entry = ledger.status()["entries"][0]
                assert entry["status"] == "failed"
                assert entry["tokens"] == entry["tokens_reserved"]
            finally:
                await backend.close()

        asyncio.run(go())


def test_overflowing_price_estimates_never_create_nonfinite_ledger_state():
    from types import SimpleNamespace

    config = SimpleNamespace(model_id="fixture", input_cost_per_million=1e308,
                             output_cost_per_million=1e308, request_cost_usd=0)
    ledger = BudgetLedger()
    with pytest.raises(BudgetExceeded, match="pricing"):
        ledger.reserve(config, [], 128)
    assert ledger.status()["requests"] == 0
    json.dumps(ledger.status(), allow_nan=False)


@pytest.mark.parametrize("method", ["openai", "qwen"])
def test_filtered_completion_does_not_expose_calls_or_private_error_payload(method):
    choice = {"finish_reason": "content_filter", "message" if method == "openai" else "delta": {
        "tool_calls": [{"index": 0, "id": "partial", "function": {"name": "write_file", "arguments": "{}"}}]}}
    data = {"choices": [choice]}
    backend = backend_for(method, lambda request: httpx.Response(200,
        text="data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n") if method == "qwen" else httpx.Response(200, json=data))

    async def go():
        try:
            with pytest.raises(RuntimeError, match="filtered"):
                await backend.generate([])
        finally:
            await backend.close()

    asyncio.run(go())

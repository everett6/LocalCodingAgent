"""Untrusted usage numbers cannot overflow accounting or refund reservations."""
import asyncio

import httpx
import pytest

from local_coder.context.telemetry import ContextTelemetry
from local_coder.core.budgets import BudgetExceeded, BudgetLedger, BudgetedModel, request_telemetry
from local_coder.types import ModelConfig, ModelResponse
from tests.test_provider_completion_integrity import backend_for


@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
def test_enormous_provider_usage_keeps_the_reservation_and_unknown_telemetry(tmp_path, kind):
    enormous = 10 ** 400
    data = ({'content': [{'type': 'text', 'text': 'fixture'}], 'stop_reason': 'end_turn',
             'usage': {'input_tokens': enormous, 'output_tokens': 1}}
            if kind == 'anthropic' else {'choices': [{'message': {'content': 'fixture'}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': enormous, 'completion_tokens': 1, 'total_tokens': enormous + 1}})
    backend = backend_for(kind, lambda request: httpx.Response(200, json=data))
    ledger = BudgetLedger(state_path=tmp_path / 'budget.json')
    telemetry = ContextTelemetry(tmp_path)

    async def scenario():
        try:
            with request_telemetry(telemetry, 'fixture'):
                await BudgetedModel(backend, ledger).generate([], max_tokens=128)
        finally:
            await backend.close()
    asyncio.run(scenario())
    entry = ledger.status()['entries'][0]
    assert entry['status'] == 'completed'
    assert entry['usage'] == 'conservative_reservation'
    assert entry['tokens'] == entry['tokens_reserved']
    assert telemetry.inspect()['requests'][0]['usage'] == {'prompt_tokens': None, 'completion_tokens': None}


def test_custom_backend_usage_cannot_bypass_the_settlement_bound(tmp_path):
    class CustomBackend:
        config = ModelConfig(name='fixture', model_id='fixture', input_cost_per_million=1,
                             output_cost_per_million=1, max_tokens=128)

        async def generate(self, *args, **kwargs):
            return ModelResponse(prompt_tokens=10 ** 400, completion_tokens=1, usage_reported=True)

    ledger = BudgetLedger(state_path=tmp_path / 'budget.json')
    telemetry = ContextTelemetry(tmp_path)
    async def scenario():
        with request_telemetry(telemetry, 'fixture'):
            await BudgetedModel(CustomBackend(), ledger).generate([], max_tokens=128)
    asyncio.run(scenario())
    entry = ledger.status()['entries'][0]
    assert entry['tokens'] == entry['tokens_reserved']
    assert entry['usage'] == 'conservative_reservation'
    assert telemetry.inspect()['requests'][0]['usage'] == {'prompt_tokens': None, 'completion_tokens': None}


def test_telemetry_numeric_overflow_cannot_erase_prior_diagnostics(tmp_path):
    telemetry = ContextTelemetry(tmp_path)
    first = telemetry.record_request([], None, 128, task_id='first')
    telemetry.record_usage(first['id'], {'prompt_tokens': 1, 'completion_tokens': 1},
                           pricing={'input_cost_per_million': 1, 'output_cost_per_million': 1, 'request_cost_usd': 0})
    second = telemetry.record_request([], None, 128, task_id='second')
    telemetry.record_usage(second['id'], {'prompt_tokens': 10 ** 400, 'completion_tokens': 1},
                           pricing={'input_cost_per_million': 1, 'output_cost_per_million': 1, 'request_cost_usd': 0})
    third = telemetry.record_request([], None, 128, task_id='third')
    telemetry.record_usage(third['id'], {'prompt_tokens': 10 ** 12, 'completion_tokens': 1},
                           pricing={'input_cost_per_million': 1e300, 'output_cost_per_million': 1, 'request_cost_usd': 0})
    requests = telemetry.inspect()['requests']
    assert len(requests) == 3
    by_task = {request['task_id']: request for request in requests}
    assert by_task['first']['cost_usd'] == pytest.approx(.000002)
    assert by_task['second']['usage']['prompt_tokens'] is None
    assert by_task['second']['cost_usd'] is None and by_task['third']['cost_usd'] is None


def test_huge_integer_pricing_is_rejected_before_admission():
    from types import SimpleNamespace
    config = SimpleNamespace(model_id='fixture', input_cost_per_million=10 ** 400,
                             output_cost_per_million=1, request_cost_usd=0)
    ledger = BudgetLedger()
    with pytest.raises(BudgetExceeded, match='pricing'):
        ledger.reserve(config, [], 128)
    assert ledger.status()['requests'] == 0

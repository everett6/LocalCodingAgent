"""Free quota admission survives transport, agents and checkpoint recovery."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from local_coder.agents.base import BaseAgent
from local_coder.core.budgets import BudgetLedger, BudgetedModel, BudgetExceeded
from local_coder.models.manager import ModelManager
from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.providers.quota import FreeQuotaGuard, ProviderQuotaExhausted
from local_coder.types import AgentRole, AgentTask, Message, ModelBackend, ModelConfig, ModelResponse, ProjectConfig, TaskStatus


class Catalog:
    def __init__(self, remaining):
        self.remaining = remaining
    def openrouter_usage(self, config):
        return {"free_model_daily_requests": {"used": 10 - self.remaining, "limit": 10, "remaining": self.remaining}}
    def openrouter_models(self):
        return {"models": []}


def selected(identity="example/model:free"):
    return ModelConfig(name="selected", model_id=identity, backend=ModelBackend.OPENAI_COMPAT,
                       base_url="https://openrouter.ai/api/v1", max_tokens=256,
                       input_cost_per_million=0, output_cost_per_million=0)


class Model:
    def __init__(self, config=None):
        self.config = config or selected()
        self.calls = 0
    async def generate(self, messages, **kwargs):
        self.calls += 1
        return ModelResponse(content="Checked the fixture.", prompt_tokens=10, completion_tokens=5)
    async def stream(self, messages, **kwargs):
        self.calls += 1
        yield "Checked"


class FixtureAgent(BaseAgent):
    role = AgentRole.CODER
    system_prompt = "Inspect the local fixture without changing it."


def test_quota_blocks_before_budget_reservation_and_actual_inference():
    async def scenario():
        model, ledger = Model(), BudgetLedger()
        wrapped = BudgetedModel(model, ledger, quota_guard=FreeQuotaGuard(Catalog(0)))
        with pytest.raises(ProviderQuotaExhausted):
            await wrapped.generate([Message(role="user", content="inspect")])
        assert model.calls == 0 and ledger.status()["requests"] == 0
        with pytest.raises(ProviderQuotaExhausted):
            async for _ in wrapped.stream([Message(role="user", content="inspect")]):
                pass
        assert model.calls == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('streaming', [False, True])
def test_local_budget_rejection_does_not_consume_daily_free_allowance(streaming):
    async def scenario():
        guard = FreeQuotaGuard(Catalog(2))
        model = Model()
        ledger = BudgetLedger(max_request_tokens=600)
        wrapped = BudgetedModel(model, ledger, quota_guard=guard)
        with pytest.raises(BudgetExceeded, match='Per-request'):
            if streaming:
                async for _ in wrapped.stream([Message(role='user', content='x' * 1000)]):
                    pass
            else:
                await wrapped.generate([Message(role='user', content='x' * 1000)])
        assert guard.status()['attempted_requests'] == 0
        assert model.calls == 0 and ledger.status()['requests'] == 0
        assert guard.status(selected(), refresh=True)['remaining'] == 2
    asyncio.run(scenario())


def test_completed_and_failed_backend_reservations_cannot_be_released():
    ledger = BudgetLedger()
    completed = ledger.reserve(selected(), [], 1)
    ledger.settle(completed, ModelResponse(content='ok', prompt_tokens=1, completion_tokens=1), config=selected())
    failed = ledger.reserve(selected(), [], 1)
    ledger.settle(failed, error=True, config=selected())
    assert ledger.release_unattempted(completed) is False
    assert ledger.release_unattempted(failed) is False
    assert ledger.status()['requests'] == 2


def test_quota_only_wrapper_and_stream_consume_exactly_one_attempt():
    async def scenario():
        guard, model = FreeQuotaGuard(Catalog(2)), Model()
        wrapped = BudgetedModel(model, None, quota_guard=guard)
        await wrapped.generate([Message(role="user", content="inspect")])
        assert [text async for text in wrapped.stream([Message(role="user", content="inspect")])] == ["Checked"]
        assert guard.status()["attempted_requests"] == 2
        with pytest.raises(ProviderQuotaExhausted):
            await wrapped.generate([])
        assert model.calls == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("streaming", [False, True])
def test_backend_http_daily_429_is_typed_and_provider_429_is_not(streaming):
    async def scenario():
        for daily in (False, True):
            guard = FreeQuotaGuard(Catalog(2))
            backend = OpenAICompatibleBackend(selected(), quota_guard=guard)
            await backend.client.aclose()
            body = {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day" if daily else "Provider temporarily limited"}}
            backend.client = httpx.AsyncClient(base_url=backend.config.base_url, transport=httpx.MockTransport(lambda request: httpx.Response(429, json=body)))
            try:
                with pytest.raises(ProviderQuotaExhausted if daily else RuntimeError) as caught:
                    if streaming:
                        async for _ in backend.stream([]):
                            pass
                    else:
                        await backend.generate([])
                assert isinstance(caught.value, ProviderQuotaExhausted) is daily
            finally:
                await backend.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["stream", "qwen_generate", "generate"])
def test_embedded_daily_quota_error_is_typed_for_every_generation_path(method):
    async def scenario():
        config = selected()
        if method == "qwen_generate":
            config.profile = "qwen38"
        backend = OpenAICompatibleBackend(config, quota_guard=FreeQuotaGuard(Catalog(2)))
        await backend.client.aclose()
        body = {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day"}}
        def respond(request):
            if method == "generate":
                return httpx.Response(200, json=body)
            return httpx.Response(200, text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        backend.client = httpx.AsyncClient(base_url=config.base_url, transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ProviderQuotaExhausted):
                if method == "stream":
                    async for _ in backend.stream([]):
                        pass
                else:
                    await backend.generate([])
        finally:
            await backend.close()
    asyncio.run(scenario())


def test_manager_shares_quota_guard_across_models_even_without_budget():
    async def scenario():
        guard = FreeQuotaGuard(Catalog(1))
        config = ProjectConfig(models={"one": selected(), "two": selected("other/model:free")})
        manager = ModelManager(config, quota_guard=guard)
        manager._create_backend = lambda config: Model(config)
        await manager.initialize()
        try:
            await manager._models["one"].generate([])
            with pytest.raises(ProviderQuotaExhausted):
                await manager._models["two"].generate([])
            assert manager._models["two"].model.calls == 0
        finally:
            await manager.close_all()
    asyncio.run(scenario())


def test_agent_pauses_without_retry_and_checkpoint_is_resumable(tmp_path):
    from local_coder.tools import create_tool_registry
    from local_coder.core.runtime import AgentRuntime
    async def scenario():
        registry = create_tool_registry(str(tmp_path))
        runtime = AgentRuntime(str(tmp_path), checkpoint_interval=1)
        registry.runtime = runtime
        guard = FreeQuotaGuard(Catalog(0))
        model = Model()
        agent = FixtureAgent(BudgetedModel(model, None, quota_guard=guard), registry)
        events = []
        agent._event_callback = events.append
        try:
            with pytest.raises(ProviderQuotaExhausted) as caught:
                await agent.execute(AgentTask(role=AgentRole.CODER, objective="Inspect fixture"))
            assert model.calls == 0
            assert not any(event.event_type == "model_retry" for event in events)
            checkpoint = caught.value.data["checkpoint"]
            saved = json.loads(open(checkpoint).read())
            assert saved["notes"]["quota_paused"] is True
            assert saved["notes"]["model_identity"]["model_id"] == model.config.model_id
            run_id = saved["state"]["run_id"]
            run = await runtime.recover_from_checkpoint(run_id, checkpoint)
            run.validate_resume()
            assert run.state.iteration == 0 and run.metrics.model_calls == 0
            assert run.state.messages
            await runtime.release_run(run_id)
        finally:
            await registry.close()
    asyncio.run(scenario())


def test_coordinator_does_not_turn_quota_into_failed_task_or_escalation(tmp_path):
    async def scenario():
        guard = FreeQuotaGuard(Catalog(0))
        coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path), quota_guard=guard)
        async def get_model(*args, **kwargs):
            return BudgetedModel(Model(), None, quota_guard=guard)
        coordinator.model_manager.get_model = get_model
        try:
            with pytest.raises(ProviderQuotaExhausted):
                await coordinator._attempt_dag_task(AgentTask(role=AgentRole.CODER, objective="Inspect"), False)
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_dollar_reservations_and_settlement_include_fixed_request_fee():
    config = SimpleNamespace(model_id="paid", input_cost_per_million=0, output_cost_per_million=0, request_cost_usd=.25)
    ledger = BudgetLedger(max_cost_usd=.3)
    identity = ledger.reserve(config, [], 1)
    ledger.settle(identity, ModelResponse(content="ok", prompt_tokens=1, completion_tokens=1), config=config)
    assert ledger.status()["cost_usd"] == .25
    with pytest.raises(BudgetExceeded):
        ledger.reserve(config, [], 1)
    config.request_cost_usd = float("nan")
    with pytest.raises(BudgetExceeded):
        BudgetLedger().reserve(config, [], 1)


def test_paused_checkpoint_model_switch_requires_explicit_resume_flag(tmp_path):
    async def scenario():
        config = ProjectConfig(project_root=str(tmp_path), models={"selected": selected()})
        guard = FreeQuotaGuard(Catalog(0))
        first = Coordinator(config, str(tmp_path), quota_guard=guard)
        first.model_manager._create_backend = lambda config: Model(config)
        try:
            with pytest.raises(ProviderQuotaExhausted) as caught:
                await first._attempt_dag_task(AgentTask(role=AgentRole.CODER, objective="Inspect saved fixture"), False)
            checkpoint = caught.value.data["checkpoint"]
        finally:
            await first.model_manager.close_all()
        changed = ProjectConfig(project_root=str(tmp_path), models={"selected": selected("explicitly-chosen/paid")})
        resumed = Coordinator(changed, str(tmp_path), quota_guard=guard)
        resumed.model_manager._create_backend = lambda config: Model(config)
        try:
            with pytest.raises(ValueError, match="model changed"):
                await resumed.resume_checkpoint(checkpoint)
            assert resumed.model_manager._models["selected"].model.calls == 0
            result = await resumed.resume_checkpoint(checkpoint, allow_model_change=True)
            assert result.status == TaskStatus.COMPLETED
            assert resumed.model_manager._models["selected"].model.calls == 1
        finally:
            await resumed.model_manager.close_all()
    asyncio.run(scenario())


def test_model_change_flag_is_rejected_for_an_ordinary_checkpoint(tmp_path):
    class Crash(BaseException):
        pass
    class CrashingModel(Model):
        async def generate(self, messages, **kwargs):
            raise Crash()
    async def scenario():
        config = ProjectConfig(project_root=str(tmp_path), models={"selected": selected()})
        coordinator = Coordinator(config, str(tmp_path))
        coordinator.model_manager._create_backend = lambda config: CrashingModel(config)
        try:
            with pytest.raises(Crash):
                await coordinator._attempt_dag_task(AgentTask(role=AgentRole.CODER, objective="Inspect fixture"), False)
            checkpoint = next(coordinator.runtime.state_dir.glob('checkpoint_*_0.json'))
            with pytest.raises(ValueError, match="quota-paused"):
                await coordinator.resume_checkpoint(str(checkpoint), allow_model_change=True)
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_explicit_smaller_model_resume_keeps_new_policy_limits(tmp_path):
    from local_coder.core.runtime import AgentRuntime
    from local_coder.types import AgentResponse
    async def scenario():
        runtime = AgentRuntime(str(tmp_path), checkpoint_interval=1)
        registry = SimpleNamespace(runtime=runtime, get_schemas_for_role=lambda role: [])
        previous = FixtureAgent(BudgetedModel(Model(), None, quota_guard=FreeQuotaGuard(Catalog(0))), registry,
                                context_window_chars=60000, compact_context_chars=30000, max_tool_output_chars=9000)
        with pytest.raises(ProviderQuotaExhausted) as caught:
            await previous.execute(AgentTask(role=AgentRole.CODER, objective="Inspect fixture"))
        checkpoint = caught.value.data['checkpoint']
        recovered = await runtime.recover_from_checkpoint(previous.state.run_id, checkpoint)
        recovered.notes['last_prompt_tokens'] = 100000
        config = selected('tiny/local-model')
        config.context_length = 4096
        current = FixtureAgent(Model(config), registry, context_window_chars=6000,
                               compact_context_chars=3000, max_tool_output_chars=1200)
        current.max_tool_output_lines = 80
        current.strategy_attempt_limit = 2
        async def inspect_policy(task, draft, resumed=False):
            assert resumed
            assert current.context_window_chars == 6000
            assert current.compact_context_chars == 3000
            assert current.max_tool_output_chars == 1200
            assert current.max_tool_output_lines == 80
            assert current.strategy_attempt_limit == 2
            assert current._last_prompt_tokens == 0
            return AgentResponse(task_id=task.task_id, status=TaskStatus.COMPLETED, summary='Done', tests_passed=True)
        current._execute_loop = inspect_policy
        await current.resume(recovered, allow_model_change=True)
    asyncio.run(scenario())


def test_quota_exception_cancels_sibling_dag_tasks_before_return(tmp_path):
    from local_coder.types import TaskPlan
    async def scenario():
        config = ProjectConfig(project_root=str(tmp_path))
        config.agentic.max_parallel_agents = 2
        coordinator = Coordinator(config, str(tmp_path))
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def run_task(dag, task, semaphore, checkpoint):
            if task.task_id == 'first':
                await started.wait()
                raise ProviderQuotaExhausted({'requires_human_action': True})
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        coordinator._run_dag_task = run_task
        plan = TaskPlan(objective='Parallel fixture', tasks=[AgentTask(task_id='first', role=AgentRole.CODER, objective='One'), AgentTask(task_id='second', role=AgentRole.CODER, objective='Two')])
        try:
            with pytest.raises(ProviderQuotaExhausted):
                await coordinator._execute_plan(plan)
            assert cancelled.is_set()
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_paid_model_free_verification_uses_catalog_cache_without_network():
    class CacheOnlyCatalog(Catalog):
        def cached_model(self, identity):
            if identity == 'openrouter/free':
                return {'id': identity, 'is_free': True, 'pricing_complete': True}
            return None
        def openrouter_models(self):
            raise AssertionError('Implicit public catalog fetch is forbidden')
    service = CacheOnlyCatalog(0)
    guard = FreeQuotaGuard(service)
    assert guard.admit(selected('paid/model'))['free'] is False
    with pytest.raises(ProviderQuotaExhausted):
        guard.admit(selected('openrouter/free'))

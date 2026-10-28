"""The production improve hook uses a bounded dedicated configured backend."""
import asyncio
import json

import pytest

from local_coder.harness.evaluation import CASES, detect
from local_coder.models.manager import ModelManager
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.providers.quota import ProviderQuotaExhausted
from local_coder.types import ModelBackend, ModelConfig, ModelResponse, ProjectConfig, SafetyTier, ToolName


def configuration(tmp_path, *, cloud=False, cap=None, unknown_price=False):
    selected = ModelConfig(name='security', model_id='qwen-fixture', backend=ModelBackend.OPENAI_COMPAT,
        base_url='https://provider.invalid/api' if cloud else 'http://127.0.0.1:9090/v1',
        input_cost_per_million=None if unknown_price else 0, output_cost_per_million=0, request_cost_usd=0)
    other = selected.model_copy(update={'name': 'unrelated', 'model_id': 'unrelated-cloud', 'base_url': 'https://other.invalid'})
    config = ProjectConfig(project_root=str(tmp_path), models={'security': selected, 'unrelated': other}, safety_tier=SafetyTier.TIER_4_AUTO_VALID)
    config.routing.fallbacks = {'security': ['unrelated']}
    config.harness.max_cost_usd = cap
    return config


class FixtureBackend:
    def __init__(self, config, *, usage=True):
        self.config = config
        self.usage = usage
        self.messages = []
        self.closed = 0
    async def generate(self, messages, **kwargs):
        self.messages.append(messages)
        return ModelResponse(content=json.dumps(detect(messages[-1].content)), prompt_tokens=20 if self.usage else 0, completion_tokens=3 if self.usage else 0)
    async def is_available(self):
        raise AssertionError('Fallback availability probes are prohibited')
    async def close(self):
        self.closed += 1


def install_backends(monkeypatch, *, usage=True):
    backends = []
    def create(manager, config):
        backend = FixtureBackend(config, usage=usage)
        backends.append(backend)
        return backend
    monkeypatch.setattr(ModelManager, '_create_backend', create)
    return backends


def test_host_hook_is_inert_until_called_and_shares_admission_and_cleanup(tmp_path, monkeypatch):
    async def scenario():
        class Quota:
            def __init__(self): self.calls = 0
            def admit(self, config): self.calls += 1
        guard = Quota()
        backends = install_backends(monkeypatch)
        coordinator = Coordinator(configuration(tmp_path), str(tmp_path), quota_guard=guard)
        tool = coordinator.tool_registry.get_tool(ToolName.HARNESS_EVOLVE)
        assert tool.improvement_evaluator == coordinator._evaluate_harness_profile
        assert not backends and guard.calls == 0
        original_closed = []
        async def close_original(): original_closed.append(True)
        coordinator.model_manager.on_close = close_original
        calls = []
        reserve = coordinator.budget.reserve
        def observe(config, messages, *args, **kwargs):
            calls.append(messages[0].content)
            return reserve(config, messages, *args, **kwargs)
        monkeypatch.setattr(coordinator.budget, 'reserve', observe)
        result = await tool.improvement_evaluator({'strategy': 'small_steps'})
        assert result['measurements']['model_calls'] == len(CASES)
        assert result['measurements']['total_tokens'] == 23 * len(CASES)
        assert coordinator.budget.status()['requests'] == len(CASES)
        assert guard.calls == len(CASES)  # No doubled wrapper reservation/admission.
        assert len(backends) == 1 and backends[0].config.name == 'security'
        assert backends[0].closed == 1
        assert not original_closed
        assert coordinator.model_manager.config.routing.fallbacks == {'security': ['unrelated']}
        assert not coordinator.model_manager._models
        assert all('"strategy": "small_steps"' in prompt for prompt in calls)
        assert calls[0] == backends[0].messages[0][0].content
        assert result['report']['benchmark'] == 'instruction_profile_fixed_suite'
        assert 'complete coding' in result['report']['limitation']
    asyncio.run(scenario())


def test_overlay_changes_the_prompt_before_shared_budget_reserves(tmp_path, monkeypatch):
    async def scenario():
        backends = install_backends(monkeypatch)
        coordinator = Coordinator(configuration(tmp_path), str(tmp_path))
        admitted = []
        reserve = coordinator.budget.reserve
        def observe(config, messages, *args, **kwargs):
            admitted.append(messages[0].content)
            return reserve(config, messages, *args, **kwargs)
        monkeypatch.setattr(coordinator.budget, 'reserve', observe)
        await coordinator._evaluate_harness_profile({})
        await coordinator._evaluate_harness_profile({'strategy': 'small_steps', 'middleware': ['verification']})
        assert admitted[0] != admitted[len(CASES)]
        assert '"strategy": "hypothesis_then_test"' in admitted[0]
        assert '"strategy": "small_steps"' in admitted[len(CASES)]
        assert '"family": "qwen"' in admitted[0]
        assert len(backends) == 2 and all(model.closed == 1 for model in backends)
    asyncio.run(scenario())


@pytest.mark.parametrize('cloud,cap,unknown_price', [(True, None, False), (True, 0, False), (False, None, True)])
def test_no_spending_admission_means_no_backend_or_dedicated_manager_construction(tmp_path, monkeypatch, cloud, cap, unknown_price):
    async def scenario():
        backends = install_backends(monkeypatch)
        coordinator = Coordinator(configuration(tmp_path, cloud=cloud, cap=cap, unknown_price=unknown_price), str(tmp_path))
        def forbidden(*args, **kwargs):
            raise AssertionError('Must reject before dedicated manager construction')
        monkeypatch.setattr('local_coder.orchestrator.coordinator.ModelManager', forbidden)
        with pytest.raises(ValueError, match='positive configured harness dollar cap'):
            await coordinator._evaluate_harness_profile({})
        assert not backends
    asyncio.run(scenario())


def test_unknown_usage_prevents_paired_validation_and_activation(tmp_path, monkeypatch):
    async def scenario():
        backends = install_backends(monkeypatch, usage=False)
        coordinator = Coordinator(configuration(tmp_path), str(tmp_path))
        tool = coordinator.tool_registry.get_tool(ToolName.HARNESS_EVOLVE)
        result = await tool.execute(action='improve', candidates=[{'strategy': 'small_steps'}])
        assert not result.success and 'usage unavailable' in result.output
        assert not tool.store.active_revisions()
        assert backends[0].closed == 1
    asyncio.run(scenario())


def test_quota_rejection_closes_only_dedicated_backend(tmp_path, monkeypatch):
    async def scenario():
        class Quota:
            def admit(self, config):
                raise ProviderQuotaExhausted({'blocked': True})
        backends = install_backends(monkeypatch)
        coordinator = Coordinator(configuration(tmp_path), str(tmp_path), quota_guard=Quota())
        with pytest.raises(ProviderQuotaExhausted):
            await coordinator._evaluate_harness_profile({})
        assert len(backends) == 1 and backends[0].closed == 1 and not backends[0].messages
        assert coordinator.budget.status()['requests'] == 0
    asyncio.run(scenario())


def test_approved_improve_action_runs_real_paired_hook_without_activation(tmp_path, monkeypatch):
    async def scenario():
        backends = install_backends(monkeypatch)
        coordinator = Coordinator(configuration(tmp_path, cloud=True, cap=1), str(tmp_path))
        tool = coordinator.tool_registry.get_tool(ToolName.HARNESS_EVOLVE)
        result = await tool.execute(action='improve', candidates=[{'strategy': 'small_steps'}])
        assert result.success
        assert result.data['baseline']['model_calls'] == len(CASES)
        assert result.data['revisions'][0]['state'] == 'rejected'  # Equal scores and usage prove no improvement.
        assert not result.data['activated'] and not tool.store.active_revisions()
        assert coordinator.budget.status()['requests'] == len(CASES) * 2
        assert len(backends) == 2 and all(model.closed == 1 for model in backends)
    asyncio.run(scenario())


def test_invalid_controls_reject_before_model_construction(tmp_path, monkeypatch):
    async def scenario():
        backends = install_backends(monkeypatch)
        coordinator = Coordinator(configuration(tmp_path), str(tmp_path))
        with pytest.raises(ValueError, match='Unknown profile controls'):
            await coordinator._evaluate_harness_profile({'budget': 'disabled'})
        assert not backends
    asyncio.run(scenario())


def test_cancelled_profile_evaluation_awaits_dedicated_cleanup(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        backends = []
        class Waiting(FixtureBackend):
            async def generate(self, messages, **kwargs):
                entered.set()
                await asyncio.Event().wait()
        def construct(manager, config):
            model = Waiting(config)
            backends.append(model)
            return model
        monkeypatch.setattr(ModelManager, '_create_backend', construct)
        coordinator = Coordinator(configuration(tmp_path), str(tmp_path))
        task = asyncio.create_task(coordinator._evaluate_harness_profile({}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backends[0].closed == 1
        assert coordinator.budget.status()['entries'][0]['status'] == 'failed'
        assert not coordinator.model_manager._models
    asyncio.run(scenario())

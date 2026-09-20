"""Tests for delta-focused predictive drafting and online feedback."""
import asyncio
from types import SimpleNamespace

from local_coder.models.cache import PromptCache
from local_coder.speculative.drafter import DeltaAttention, PredictionPolicy, SpeculativeDrafter
from local_coder.types import ModelResponse


def test_delta_attention_prioritizes_changes_and_objective_terms():
    content = "\n".join([
        "def unrelated():",
        "    return 1",
        "+    timeout = 30",
        "    return timeout",
    ])

    focused = DeltaAttention().focus("fix timeout", content, max_lines=2)

    assert "timeout" in focused
    assert "+    timeout = 30" in focused


def test_prediction_policy_rewards_useful_predictions():
    policy = PredictionPolicy()

    initial = policy.confidence("edit")
    policy.record("edit", 5.0)
    improved = policy.confidence("edit")

    assert improved > initial
    assert policy.should_predict("edit", 0.5)


def test_prediction_policy_disables_repeatedly_unhelpful_type():
    policy = PredictionPolicy()

    for _ in range(8):
        policy.record("action", -2.0)

    assert policy.confidence("action") < 0.5
    assert not policy.should_predict("action", 0.5)


def test_prompt_cache_reuses_drafter_response():
    class FakeModel:
        config = SimpleNamespace(model_id="draft", backend=SimpleNamespace(value="local"))

        def __init__(self):
            self.calls = 0

        async def generate(self, messages):
            self.calls += 1
            return ModelResponse(content="cached patch")

    model = FakeModel()
    drafter = SpeculativeDrafter(model, prompt_cache=PromptCache())

    first = asyncio.run(drafter.predict_edit("app.py", "return 1", "fix return"))
    second = asyncio.run(drafter.predict_edit("app.py", "return 1", "fix return"))

    assert first is not None and second is not None
    assert model.calls == 1
    assert drafter.get_stats().cache_hits == 1


def test_prompt_cache_expires_entries():
    cache = PromptCache(ttl_seconds=0)
    cache.set("key", "value")

    assert cache.get("key") is None
    assert cache.misses == 1
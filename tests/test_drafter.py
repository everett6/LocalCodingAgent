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


def test_delta_attention_includes_surrounding_context_not_isolated_lines():
    """A changed line taken alone loses its meaning -- the function it's in
    should come along with it, not just the single highest-scoring line."""
    content = "\n".join([
        "def process_timeout():",           # 0
        "    x = 1",                         # 1
        "    y = 2",                         # 2
        "+    timeout = 30",                 # 3
        "    return timeout",                # 4
        "    z = 99",                        # 5
    ])

    focused = DeltaAttention().focus("fix timeout", content, max_lines=10, context_radius=2)

    # The changed line's immediate neighbors (y=2 above, return below) must
    # be present, not just the isolated "+    timeout = 30" line.
    assert "y = 2" in focused
    assert "return timeout" in focused


def test_delta_attention_uses_word_boundaries_not_substring_matches():
    """A plain substring check on "class" would also match "subclass" or
    "classify" -- objective-term matching must respect word boundaries."""
    content = "\n".join([
        "def subclass_helper():",   # false-positive risk: contains "class"
        "    return 1",
        "class Foo:",               # the actual match
        "    pass",
    ])

    focused = DeltaAttention().focus("fix class", content, max_lines=1, context_radius=0)

    assert focused.strip() == "3: class Foo:"


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


def test_prompt_cache_evicts_on_entry_count():
    cache = PromptCache(max_entries=2)
    cache.set("a", "1")
    cache.set("b", "2")
    cache.set("c", "3")

    assert cache.get("a") is None
    assert cache.get("b") == "2"
    assert cache.get("c") == "3"


def test_prompt_cache_evicts_on_total_size_even_with_room_on_entry_count():
    """Entry count alone doesn't bound memory: one oversized value (e.g. from
    a role configured with a large max_tokens) must still get evicted even
    though there's plenty of room left under max_entries."""
    cache = PromptCache(max_entries=100, max_total_chars=55)
    cache.set("small", "x" * 10)

    cache.set("big", "y" * 50)

    assert cache.get("small") is None  # evicted to make room under max_total_chars
    assert cache.get("big") == "y" * 50


def test_prompt_cache_key_is_stable_for_identical_prompts_and_differs_otherwise():
    from types import SimpleNamespace
    from local_coder.models.cache import prompt_cache_key
    from local_coder.types import Message

    config = SimpleNamespace(model_id="draft", backend=SimpleNamespace(value="local"))
    messages = [Message(role="user", content="fix the bug")]

    key1 = prompt_cache_key(config, messages)
    key2 = prompt_cache_key(config, [Message(role="user", content="fix the bug")])
    key3 = prompt_cache_key(config, [Message(role="user", content="fix a different bug")])

    assert key1 == key2
    assert key1 != key3
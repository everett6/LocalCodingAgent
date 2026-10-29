from types import SimpleNamespace

import pytest

from local_coder.providers.context_policy import context_policy
from local_coder.types import AgentMode, HarnessSettings, ModelConfig


def model(**changes):
    return SimpleNamespace(
        context_length=1048576,
        max_tokens=262144,
        context_source="openrouter_catalog",
        auto_context=True,
        **changes,
    )


@pytest.mark.parametrize("mode", list(AgentMode))
def test_policy_reserves_output_and_overhead_within_physical_and_harness_limits(mode):
    config = model()
    harness = HarnessSettings(max_request_tokens=65536, max_tokens=1000000)
    policy = context_policy(config, mode, harness)
    assert policy["model_limit"] == 1048576
    assert policy["mode"] == mode.value and policy["source"] == "openrouter_catalog"
    assert (
        policy["prompt_tokens"]
        + policy["completion_tokens"]
        + policy["reserved_overhead_tokens"]
        <= 65536
    )
    assert policy["context_window_chars"] * 4 <= policy["prompt_tokens"]
    assert 0 < policy["compact_context_chars"] < policy["context_window_chars"]
    assert policy["completion_tokens"] <= config.max_tokens
    assert policy["memory_chars"] <= harness.memory_context_chars


def test_modes_have_distinct_prompt_and_completion_allocations():
    policies = [context_policy(model(), mode) for mode in AgentMode]
    assert len({row["prompt_tokens"] for row in policies}) == len(AgentMode)
    assert policies[0]["completion_tokens"] > policies[1]["completion_tokens"]


@pytest.mark.parametrize("context", [32, 128, 1024, 4096])
def test_small_model_context_produces_positive_bounded_windows(context):
    config = SimpleNamespace(
        context_length=context, max_tokens=1000000, context_source="local_runtime"
    )
    policy = context_policy(config)
    assert policy["prompt_tokens"] > 0 and policy["completion_tokens"] > 0
    assert policy["context_window_chars"] > 0 and policy["compact_context_chars"] > 0
    assert (
        policy["prompt_tokens"]
        + policy["completion_tokens"]
        + policy["reserved_overhead_tokens"]
        <= context
    )


def test_explicit_small_output_budget_is_honored_and_memory_can_be_disabled():
    config = SimpleNamespace(
        context_length=32768, max_tokens=123, context_source="configured"
    )
    policy = context_policy(config, "coding", HarnessSettings(memory_enabled=False))
    assert policy["completion_tokens"] == 123 and policy["memory_chars"] == 0


def test_total_task_budget_can_narrow_request_capacity_and_function_is_pure():
    config = ModelConfig(
        name="test",
        model_id="test",
        context_length=131072,
        max_tokens=65536,
        auto_context=False,
    )
    harness = HarnessSettings(max_tokens=8192, max_request_tokens=65536)
    before = config.model_dump()
    policy = context_policy(config, "bug_hunting", harness)
    assert policy["request_limit"] == 8192
    assert config.model_dump() == before


@pytest.mark.parametrize(
    "config,mode,harness",
    [
        ({"context_length": 0, "max_tokens": 10}, "coding", None),
        ({"context_length": True, "max_tokens": 10}, "coding", None),
        ({"context_length": 8192, "max_tokens": False}, "coding", None),
        ({"context_length": 8192, "max_tokens": 10}, "made-up-mode", None),
        (
            {"context_length": 8192, "max_tokens": 10},
            "coding",
            {"max_request_tokens": "8192"},
        ),
    ],
)
def test_invalid_limits_or_modes_are_not_silently_coerced(config, mode, harness):
    with pytest.raises(ValueError):
        context_policy(config, mode, harness)

"""Tests for delta-focused predictive drafting and online feedback."""
from local_coder.speculative.drafter import DeltaAttention, PredictionPolicy


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
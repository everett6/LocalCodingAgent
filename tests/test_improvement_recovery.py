import asyncio

import pytest

from local_coder.harness.evolution import EvolutionStore
from local_coder.harness.improvement import improvement_cycle


def observation(tokens=10, **extra):
    return {"success": True, "measurements": {
        "cases": 2, "passed": 2, "total_tokens": tokens, "model_calls": 1, **extra}}


def test_failed_evaluator_does_not_skip_remaining_candidates(tmp_path):
    store = EvolutionStore(tmp_path)

    async def evaluator(overlay):
        if overlay.get("strategy") == "small_steps":
            raise RuntimeError("benchmark worker failed")
        return observation(5 if overlay else 10)

    result = asyncio.run(improvement_cycle(store, [
        {"strategy": "small_steps"}, {"strategy": "inspect_then_edit"}], evaluator))
    assert [r["state"] for r in result["revisions"]] == ["candidate", "validated"]
    assert "benchmark worker failed" in result["revisions"][0]["error"]
    assert store.active_revisions() == []


def test_cancellation_propagates_without_evaluating_next_candidate(tmp_path):
    store = EvolutionStore(tmp_path)
    calls = []

    async def evaluator(overlay):
        calls.append(overlay)
        if overlay:
            raise asyncio.CancelledError()
        return observation()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(improvement_cycle(store, [
            {"strategy": "small_steps"}, {"strategy": "inspect_then_edit"}], evaluator))
    assert len(calls) == 2
    assert len(store.history()) == 1
    assert store.active_revisions() == []


def test_input_and_reused_measurement_dictionaries_are_isolated(tmp_path):
    store = EvolutionStore(tmp_path)
    candidates = [{"preferred_tools": ["read_file"]}, {"strategy": "small_steps"}]
    shared = observation()
    calls = []

    async def evaluator(overlay):
        calls.append(overlay)
        candidates[1]["strategy"] = "hypothesis_then_test"
        if overlay:
            shared["measurements"]["total_tokens"] = 5
        return shared

    result = asyncio.run(improvement_cycle(store, candidates, evaluator))
    assert result["baseline"]["total_tokens"] == 10
    assert calls[-1] == {"strategy": "small_steps"}
    assert [r["state"] for r in result["revisions"]] == ["validated", "validated"]
    assert store.active_revisions() == []


@pytest.mark.parametrize("candidate_extra", [
    {"safety_violations": 1}, {}, {"safety_violations": 0, "other": 0},
])
def test_safety_regression_or_missing_observations_reject_candidate(tmp_path, candidate_extra):
    store = EvolutionStore(tmp_path)

    async def evaluator(overlay):
        return observation(5, **candidate_extra) if overlay else observation(safety_violations=0)

    result = asyncio.run(improvement_cycle(store, [{"strategy": "small_steps"}], evaluator))
    assert result["revisions"][0]["state"] == "rejected"
    assert store.active_revisions() == []

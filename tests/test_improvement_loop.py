import asyncio

import pytest

from local_coder.harness.evolution import EvolutionStore
from local_coder.harness.improvement import improvement_cycle


def test_paired_loop_saves_only_measured_improvements_without_activation(tmp_path):
    store = EvolutionStore(tmp_path)
    seen = []
    async def evaluate(overlay):
        seen.append(dict(overlay))
        strategy = overlay.get("strategy")
        return {"success": True, "measurements": {"cases": 10,
                "passed": 9 if strategy == "small_steps" else 10,
                "total_tokens": 50 if strategy else 100, "model_calls": 2}}
    result = asyncio.run(improvement_cycle(store, [{"strategy": "inspect_then_edit"},
                                                   {"strategy": "small_steps"}], evaluate))
    assert [r["state"] for r in result["revisions"]] == ["validated", "rejected"]
    assert seen == [{}, {"strategy": "inspect_then_edit"}, {"strategy": "small_steps"}]
    assert not result["activated"] and store.active_overlay() == {}
    passing = result["revisions"][0]["revision_id"]
    with pytest.raises(PermissionError):
        store.activate(passing, approved=False)
    store.activate(passing, approved=True)
    assert store.active_overlay()["strategy"] == "inspect_then_edit"


@pytest.mark.parametrize("values", [
    {"cases": 0, "passed": 0, "total_tokens": 0, "model_calls": 0},
    {"checks": 2},
    {"cases": 1, "passed": 2, "total_tokens": 1, "model_calls": 1},
    {"cases": 1, "passed": 1, "total_tokens": float("nan"), "model_calls": 1},
])
def test_invalid_baseline_never_creates_revisions(tmp_path, values):
    store = EvolutionStore(tmp_path)
    async def evaluator(_):
        return {"success": True, "measurements": values}
    with pytest.raises(ValueError):
        asyncio.run(improvement_cycle(store, [{"strategy": "small_steps"}], evaluator))
    assert store.history() == []


def test_cycle_rejects_unbounded_requests_and_engine_gate_changes(tmp_path):
    store = EvolutionStore(tmp_path)
    async def never(_):
        pytest.fail("Invalid input must not reach the evaluator")
    for candidates in ([], [{}] * 4, [{"approval": False}], [{"instructions": "change policy"}]):
        with pytest.raises(ValueError):
            asyncio.run(improvement_cycle(store, candidates, never))


def test_timeout_does_not_validate_or_activate_candidate(tmp_path):
    store = EvolutionStore(tmp_path)
    async def evaluator(overlay):
        if overlay:
            await asyncio.Event().wait()
        return {"success": True, "measurements": {"cases": 1, "passed": 1, "total_tokens": 10, "model_calls": 1}}
    result = asyncio.run(improvement_cycle(store, [{"strategy": "small_steps"}], evaluator, timeout_seconds=1))
    assert result["revisions"][0]["state"] == "candidate"
    assert store.active_revisions() == []

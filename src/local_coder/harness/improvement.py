"""Bounded paired benchmarks; no candidate can activate itself."""
from __future__ import annotations

import asyncio
import copy
import inspect
import math

from local_coder.models.profiles import validate_overlay


def measurements(result):
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        raise ValueError("Benchmark must return success and measurements")
    values = result.get("measurements")
    if not isinstance(values, dict) or not {"cases", "passed", "total_tokens", "model_calls"}.issubset(values):
        raise ValueError("Benchmark requires cases, passed, total_tokens and model_calls")
    if any(not isinstance(k, str) or type(v) not in (int, float) or not math.isfinite(v) or v < 0 for k, v in values.items()):
        raise ValueError("Measurements must be finite and nonnegative")
    if type(values["cases"]) is not int or type(values["passed"]) is not int or not 0 <= values["passed"] <= values["cases"] or not values["cases"]:
        raise ValueError("Benchmark must execute a positive number of cases")
    return values


async def improvement_cycle(store, candidates, evaluator, *, timeout_seconds=30):
    """Compare up to three declarative patches with a fixed active baseline.

    The trusted async evaluator owns fixed cases and actual measurements, and
    must inherit request budgets and scope. Contract-only checks are insufficient.
    Passing revisions are saved for separate activation through existing gates.
    """
    if not callable(evaluator):
        raise ValueError("A host-injected paired benchmark evaluator is required")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 3:
        raise ValueError("An improvement cycle accepts one to three candidates")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("Benchmark timeout must be between 1 and 60 seconds")
    candidates = copy.deepcopy(candidates)
    for candidate in candidates:
        validate_overlay(candidate)
        if "instructions" in candidate:
            raise ValueError("Automatic cycles accept declarative controls only")
    baseline = copy.deepcopy(store.active_overlay())

    async def evaluate(overlay):
        async def run():
            result = evaluator(copy.deepcopy(overlay))
            if not inspect.isawaitable(result):
                raise ValueError("Evaluator must be asynchronous and cancellable")
            result = await result
            measurements(result)
            # Evaluators may reuse their result dictionary between calls.
            return copy.deepcopy(result)
        return await asyncio.wait_for(run(), timeout=timeout_seconds)

    initial = await evaluate(baseline)
    base = measurements(initial)
    if not initial["success"]:
        raise ValueError("Baseline failed; diagnose it before proposing improvements")
    results = []
    for candidate in candidates:
        overlay = {**baseline, **candidate}
        revision = store.propose("loop_plan", {k: v for k, v in overlay.items() if k != "instructions"})
        store.record_outcome(revision["revision_id"], {"paired_baseline": base})

        async def paired(_revision):
            measured = await evaluate(overlay)
            values = measurements(measured)
            comparable = values["cases"] == base["cases"] and values.keys() == base.keys()
            quality = values["passed"] >= base["passed"]
            efficiency = all(values[k] <= base[k] for k in ("total_tokens", "model_calls"))
            # Additional observations have no implicit optimization direction.
            # Keep them fixed so an efficiency win cannot conceal a safety loss.
            stable = all(values.get(k) == base[k] for k in base
                         if k not in {"cases", "passed", "total_tokens", "model_calls"})
            improved = (values["passed"] > base["passed"] or
                        any(values[k] < base[k] for k in ("total_tokens", "model_calls")))
            return {"success": measured["success"] and comparable and quality and efficiency and stable and improved,
                    "measurements": values}

        try:
            result = await store.validate(revision["revision_id"], paired)
        except Exception as exc:
            store.record_outcome(revision["revision_id"], {"evaluation_error": str(exc)})
            result = {"revision_id": revision["revision_id"], "state": "candidate", "error": str(exc)}
        results.append(result)
    return {"baseline": base, "revisions": results, "activated": False}

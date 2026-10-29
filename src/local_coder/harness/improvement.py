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
    if getattr(evaluator, 'heldout_only', False):
        raise ValueError('Release held-out tasks cannot be used for adaptation')
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
            if result.get('purpose') == 'release_holdout' or result.get('report', {}).get('purpose') == 'release_holdout':
                raise ValueError('Release held-out tasks cannot be used for adaptation')
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
            if initial.get("report", {}).get("purpose") == "profile_regression":
                gate = compare_profile_reports(initial["report"], measured.get("report", {}))
                store.record_outcome(revision["revision_id"], {"profile_regression": gate})
                return {"success": measured["success"] and gate["passed"], "measurements": values}
            return {"success": measured["success"] and comparable and quality and efficiency and stable and improved,
                    "measurements": values}

        try:
            result = await store.validate(revision["revision_id"], paired)
        except Exception as exc:
            store.record_outcome(revision["revision_id"], {"evaluation_error": str(exc)})
            result = {"revision_id": revision["revision_id"], "state": "candidate", "error": str(exc)}
        results.append(result)
    return {"baseline": base, "revisions": results, "activated": False}


def compare_profile_reports(baseline: dict, candidate: dict, *, max_latency_ratio: float = 1.25) -> dict:
    """Compare trusted same-suite profile observations, never model assertions.

    Baseline/candidate must come from the host's shared-budget fixed-suite
    adapter. This pure comparison cannot authenticate arbitrary caller reports.
    Held-out reports are excluded so release tasks cannot train profile choices.
    """
    if type(max_latency_ratio) not in (int, float) or not math.isfinite(max_latency_ratio) or not 1 <= max_latency_ratio <= 2:
        raise ValueError('Latency tolerance must be between 1 and 2')
    reasons = []
    reports = (baseline, candidate)
    for report in reports:
        if not isinstance(report, dict) or report.get('mode') != 'model' or report.get('purpose') != 'profile_regression' or report.get('benchmark') != 'instruction_profile_fixed_suite':
            return {'passed': False, 'reasons': ['Require observed profile-regression model reports']}
    if baseline.get('suite_hash') != candidate.get('suite_hash') or not isinstance(baseline.get('suite_hash'), str) or len(baseline['suite_hash']) != 64:
        reasons.append('suite_mismatch')
    quality_keys = ('cases', 'passed', 'false_positives')
    observed_keys = ('model_calls', 'total_tokens', 'cost_usd', 'elapsed_seconds', 'tool_failures', 'response_errors')
    for report in reports:
        for section, keys in (('quality', quality_keys), ('observed', observed_keys)):
            values = report.get(section)
            if not isinstance(values, dict) or any(type(values.get(k)) not in (int, float) or not math.isfinite(values[k]) or values[k] < 0 for k in keys):
                return {'passed': False, 'reasons': ['Missing or invalid observed metrics']}
        quality = report['quality']
        if any(type(quality[k]) is not int for k in quality_keys) or not 0 <= quality['passed'] <= quality['cases'] or quality['cases'] == 0:
            return {'passed': False, 'reasons': ['Invalid completion counts']}
    bq, cq = baseline['quality'], candidate['quality']
    bo, co = baseline['observed'], candidate['observed']
    if bq['cases'] != cq['cases']:
        reasons.append('case_count_mismatch')
    if cq['passed'] < bq['passed']:
        reasons.append('completion_regression')
    if cq['false_positives'] > bq['false_positives']:
        reasons.append('false_positive_regression')
    for key in ('model_calls', 'total_tokens', 'cost_usd', 'tool_failures', 'response_errors'):
        if co[key] > bo[key]:
            reasons.append(key + '_regression')
    if co['elapsed_seconds'] > bo['elapsed_seconds'] * max_latency_ratio:
        reasons.append('latency_regression')
    improved = cq['passed'] > bq['passed'] or cq['false_positives'] < bq['false_positives'] or any(co[k] < bo[k] for k in observed_keys if k != 'elapsed_seconds')
    if not improved:
        reasons.append('no_measured_improvement')
    return {'passed': not reasons, 'reasons': reasons, 'max_latency_ratio': max_latency_ratio,
            'limitation': 'Narrow fixed-suite comparison. Shared host budgets and separate release holdout are required; this is not general model-quality proof.'}


async def validate_profile_revision(store, revision: dict, evaluator, *, timeout_seconds: int = 30) -> dict:
    """Host validation hook for direct loop-plan proposals before activation.

    evaluator(overlay) is the same admitted shared-budget async profile adapter
    used by improvement_cycle. This prevents propose/validate from substituting
    schema-only checks for paired measurements. It never activates revisions.
    """
    if not isinstance(revision, dict) or revision.get('kind') != 'loop_plan':
        raise ValueError('Measured profile validation requires a loop-plan revision')
    if not callable(evaluator) or getattr(evaluator, 'heldout_only', False):
        raise ValueError('An admitted profile evaluator is required; held-out tasks cannot train profiles')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError('Profile validation timeout must be between 1 and 60 seconds')
    payload = validate_overlay(revision.get('payload'))
    baseline = copy.deepcopy(store.active_overlay())
    async def evaluate(overlay):
        result = evaluator(copy.deepcopy(overlay))
        if not inspect.isawaitable(result):
            raise ValueError('Profile evaluator must be asynchronous')
        result = copy.deepcopy(await asyncio.wait_for(result, timeout_seconds))
        measurements(result)
        return result
    before = await evaluate(baseline)
    after = await evaluate({**baseline, **payload})
    gate = compare_profile_reports(before.get('report', {}), after.get('report', {}))
    gate['passed'] = gate['passed'] and before['success'] and after['success']
    store.record_outcome(revision['revision_id'], {'profile_regression': gate,
        'paired_baseline': before.get('report', {}), 'paired_candidate': after.get('report', {})})
    values = measurements(after)
    for prefix, result in (('baseline', before), ('candidate', after)):
        report = result.get('report', {})
        for key in ('cost_usd', 'elapsed_seconds', 'tool_failures', 'response_errors'):
            value = report.get('observed', {}).get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                values[prefix + '_' + key] = value
        for key in ('passed', 'false_positives'):
            value = report.get('quality', {}).get(key)
            if type(value) is int and value >= 0:
                values[prefix + '_' + key] = value
    return {'success': gate['passed'], 'measurements': values, 'gate': gate}

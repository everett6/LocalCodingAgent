"""Versioned local evaluation fixtures and measured, non-executing runners.

The offline score describes this small AST detector only. Model runners must be
explicitly supplied by a host, including its shared budget and quota guard.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import ipaddress
import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from local_coder.core.budgets import BudgetedModel
from local_coder.statedir import state_path
from local_coder.types import Message

SUITE_VERSION = "python-security-fixtures-v1"
MAX_HISTORY = 30
MAX_HISTORY_BYTES = 500_000
_STORE_LOCK = threading.RLock()


def spending_admission(config, ledger):
    """Decide before backend construction, without probing or provider fallback."""
    prices = [getattr(config, key, None) for key in ('input_cost_per_million', 'output_cost_per_million', 'request_cost_usd')]
    known_free = all(type(p) in (int, float) and math.isfinite(p) and p == 0 for p in prices)
    try:
        url = urlsplit(config.base_url)
        host = url.hostname or ''
        local = url.scheme in {'http', 'https'} and not url.username and not url.password and (host == 'localhost' or ipaddress.ip_address(host).is_loopback)
    except ValueError:
        local = False
    backend = str(getattr(getattr(config, 'backend', ''), 'value', getattr(config, 'backend', '')))
    local = local and backend in {'ollama', 'llama.cpp', 'openai_compatible', 'llamacpp', 'openai_compat'}
    cap = ledger.limits.get('max_cost_usd')
    if not (local and known_free) and not (type(cap) in (int, float) and math.isfinite(cap) and cap > 0):
        raise ValueError('Model evaluation requires a positive configured harness dollar cap for cloud, metered or unknown-cost models')


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    kind: str
    source: str
    expected: tuple[str, ...]


CASES = (
    EvaluationCase("eval-vulnerable", "vulnerable", "def parse(value):\n    return eval(value)\n", ("dynamic_eval",)),
    EvaluationCase("eval-fixed", "fixed", "import ast\ndef parse(value):\n    return ast.literal_eval(value)\n", ()),
    EvaluationCase("eval-harmless", "harmless", "def describe():\n    return 'eval(value)'\n", ()),
    EvaluationCase("shell-vulnerable", "vulnerable", "import subprocess\ndef run(value):\n    return subprocess.run(value, shell=True)\n", ("shell_execution",)),
    EvaluationCase("shell-fixed", "fixed", "import subprocess\ndef run(value):\n    return subprocess.run(['printf', '%s', value], shell=False)\n", ()),
    EvaluationCase("shell-harmless", "harmless", "class Display:\n    def run(self, value, shell=True):\n        return value\nDisplay().run('label', shell=True)\n", ()),
    EvaluationCase("pickle-vulnerable", "vulnerable", "import pickle\ndef parse(value):\n    return pickle.loads(value)\n", ("unsafe_pickle",)),
    EvaluationCase("pickle-fixed", "fixed", "import json\ndef parse(value):\n    return json.loads(value)\n", ()),
    EvaluationCase("pickle-harmless", "harmless", "def loads(value):\n    return len(value)\nloads('pickle.loads(value)')\n", ()),
)


def suite_hash():
    encoded = json.dumps({"version": SUITE_VERSION, "cases": [asdict(c) for c in CASES]}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def catalog():
    return {"version": SUITE_VERSION, "suite_hash": suite_hash(), "cases": [{"id": c.id, "kind": c.kind} for c in CASES],
            "modes": ["offline_static", "model"], "limitation": "Small synthetic Python suite; static scores do not measure model quality or establish safety."}


def detect(source):
    """Parse bounded fixture text; never import, compile or execute inspected code."""
    if not isinstance(source, str) or len(source) > 20_000:
        raise ValueError("Source exceeds the fixture parse limit")
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module in {"pickle", "subprocess"}:
            aliases.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{name(node.value)}.{node.attr}"
        return ""
    findings = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = name(node.func)
        if called == "eval":
            findings.add("dynamic_eval")
        if called in {"pickle.loads", "pickle.load"}:
            findings.add("unsafe_pickle")
        if called in {"subprocess.run", "subprocess.call", "subprocess.Popen", "subprocess.check_output", "subprocess.check_call"} and any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords):
            findings.add("shell_execution")
    return sorted(findings)


def score_results(results, *, expected_suite_hash):
    """Score labels only. Caller-supplied timing, token or cost claims are rejected."""
    if expected_suite_hash != suite_hash():
        raise ValueError("Evaluation suite hash mismatch")
    if not isinstance(results, dict) or set(results) != {c.id for c in CASES}:
        raise ValueError("Results must cover exactly the fixed suite")
    tp = fp = fn = passed = 0
    scored = []
    for case in CASES:
        labels = results[case.id]
        if not isinstance(labels, list) or len(labels) > 10 or any(not isinstance(label, str) or label not in {"dynamic_eval", "shell_execution", "unsafe_pickle"} for label in labels) or len(set(labels)) != len(labels):
            raise ValueError("Results require bounded supported finding labels")
        predicted, expected = set(labels), set(case.expected)
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        correct = predicted == expected
        passed += correct
        scored.append({"case_id": case.id, "kind": case.kind, "expected": sorted(expected), "predicted": sorted(predicted), "passed": correct})
    return {"cases": len(CASES), "passed": passed, "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None, "results": scored}


def _limits(max_cases, timeout_seconds):
    if type(max_cases) is not int or not len(CASES) <= max_cases <= 100:
        raise ValueError("Case limit must cover the fixed suite and be at most 100")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("Timeout must be between 1 and 60 seconds")


def run_offline(*, expected_suite_hash=None, max_cases=20, timeout_seconds=10):
    _limits(max_cases, timeout_seconds)
    if expected_suite_hash is not None and expected_suite_hash != suite_hash():
        raise ValueError("Evaluation suite hash mismatch")
    started = time.monotonic()
    results = {}
    for case in CASES:
        if time.monotonic() - started >= timeout_seconds:
            raise TimeoutError("Evaluation timed out")
        results[case.id] = detect(case.source)
    score = score_results(results, expected_suite_hash=suite_hash())
    return {"id": uuid.uuid4().hex, "created_at": time.time(), "mode": "offline_static", "suite_version": SUITE_VERSION,
            "suite_hash": suite_hash(), "quality": score,
            "observed": {"elapsed_seconds": time.monotonic() - started, "model_calls": 0, "total_tokens": 0, "cost_usd": 0},
            "missing_metrics": [], "limitation": catalog()["limitation"]}


def make_model_evaluator(model_factory, *, ledger, quota_guard=None, expected_suite_hash=None, timeout_seconds=30, max_cases=20, require_usage=True, render_profile=False):
    """Build a trusted async improvement adapter with fixed prompts and real calls.

    model_factory(overlay) must construct a backend without making requests. The
    provided shared ledger/guard enforce admission; no report from model text is
    accepted as resource usage. Unknown usage prevents improvement validation.
    """
    _limits(max_cases, timeout_seconds)
    if type(render_profile) is not bool:
        raise ValueError("Profile rendering must be explicitly enabled by the host")
    expected_hash = expected_suite_hash or suite_hash()
    if expected_hash != suite_hash() or ledger is None or not callable(model_factory):
        raise ValueError("A matching suite, host model factory and shared budget are required")

    async def evaluate(overlay):
        if expected_hash != suite_hash():
            raise ValueError("Evaluation suite hash mismatch")
        model = BudgetedModel(model_factory(copy.deepcopy(overlay)), ledger, quota_guard)
        instruction = 'Return only a JSON array of finding labels: dynamic_eval, shell_execution, unsafe_pickle. Return [] when none apply. Inspect the Python source without running it.'
        if render_profile:
            from local_coder.models.profiles import resolve_profile
            from local_coder.types import AgentRole
            profile = resolve_profile(model.config, AgentRole.SECURITY, overlay)
            instruction = ('Engine model-family instruction profile for this fixed-suite benchmark:\n'
                           + json.dumps(asdict(profile), sort_keys=True) + '\n\n' + instruction)
        started = time.monotonic()
        results, tokens, calls, cost, known_cost, known_usage = {}, 0, 0, 0.0, True, True
        async def run():
            nonlocal tokens, calls, cost, known_cost, known_usage
            for case in CASES:
                response = await model.generate([Message(role="system", content=instruction), Message(role="user", content=case.source)], max_tokens=128, temperature=0)
                calls += 1
                prompt, completion = getattr(response, "prompt_tokens", None), getattr(response, "completion_tokens", None)
                usage = type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0 and prompt + completion > 0
                known_usage = known_usage and usage
                if usage:
                    tokens += prompt + completion
                ip = getattr(model.config, "input_cost_per_million", None)
                op = getattr(model.config, "output_cost_per_million", None)
                rp = getattr(model.config, "request_cost_usd", 0)
                prices = all(type(p) in (int, float) and math.isfinite(p) and p >= 0 for p in (ip, op, rp))
                known_cost = known_cost and prices and usage
                if prices and usage:
                    cost += (prompt * ip + completion * op) / 1_000_000 + rp
                content = getattr(response, "content", "")
                if not isinstance(content, str) or len(content) > 4096:
                    raise ValueError("Model returned an invalid evaluation response")
                try:
                    results[case.id] = json.loads(content)
                except (ValueError, TypeError):
                    raise ValueError("Model returned an invalid evaluation response") from None
        await asyncio.wait_for(run(), timeout=timeout_seconds)
        score = score_results(results, expected_suite_hash=expected_hash)
        report = {"id": uuid.uuid4().hex, "created_at": time.time(), "mode": "model", "suite_hash": expected_hash, "suite_version": SUITE_VERSION,
                  "quality": score, "observed": {"elapsed_seconds": time.monotonic() - started, "model_calls": calls, "total_tokens": tokens if known_usage else None, "cost_usd": cost if known_cost else None},
                  "missing_metrics": ([] if known_usage else ["total_tokens"]) + ([] if known_cost else ["cost_usd"])}
        if render_profile:
            report["benchmark"] = "instruction_profile_fixed_suite"
            report["limitation"] = "Narrow model-family instruction-profile benchmark on fixed Python fixtures; this does not measure complete coding workflows or establish safety."
        if not known_usage and require_usage:
            raise ValueError("Model usage unavailable; paired improvement requires observed tokens")
        return {"success": True, "measurements": {"cases": score["cases"], "passed": score["passed"], "total_tokens": tokens if known_usage else None, "model_calls": calls}, "report": report}
    return evaluate


class EvaluationStore:
    """One bounded private atomic history file, without arbitrary report ingestion."""
    relative = ".local-coder/evaluations/history.json"

    def __init__(self, root):
        self.root = Path(root)

    def history(self):
        with _STORE_LOCK:
            path = state_path(self.root, self.relative)
            if not path.exists():
                return []
            if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > MAX_HISTORY_BYTES:
                raise ValueError("Invalid evaluation history")
            path.chmod(0o600)
            history = json.loads(path.read_text())
            if not isinstance(history, list) or len(history) > MAX_HISTORY:
                raise ValueError("Invalid evaluation history")
            # Never echo arbitrary state fields or response text from disk.
            return [self._report(item) for item in history]

    @staticmethod
    def _report(item):
        if not isinstance(item, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(item.get("id", ""))):
            raise ValueError("Invalid evaluation report")
        # Reconstruct fixed-suite scores and text; validate numeric host measurements.
        if item.get("mode") not in {"offline_static", "model"} or item.get("suite_hash") != suite_hash():
            raise ValueError("Evaluation history suite mismatch")
        quality = item.get("quality", {})
        rows = quality.get("results", []) if isinstance(quality, dict) else []
        if not isinstance(rows, list) or len(rows) != len(CASES) or any(not isinstance(row, dict) or not isinstance(row.get("case_id"), str) for row in rows):
            raise ValueError("Invalid stored evaluation results")
        predictions = {row.get("case_id"): row.get("predicted") for row in rows}
        fresh = {"id": item["id"], "mode": item["mode"], "suite_version": SUITE_VERSION,
                 "suite_hash": suite_hash(), "quality": score_results(predictions, expected_suite_hash=suite_hash()),
                 "observed": {"model_calls": 0, "total_tokens": 0, "cost_usd": 0},
                 "missing_metrics": [], "limitation": catalog()["limitation"]}
        for key in ("created_at",):
            value = item.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid evaluation timestamp")
            fresh[key] = value
        if not isinstance(item.get("observed"), dict):
            raise ValueError("Invalid evaluation observations")
        elapsed = item["observed"].get("elapsed_seconds")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 60:
            raise ValueError("Invalid evaluation duration")
        fresh["observed"]["elapsed_seconds"] = elapsed
        if item['mode'] == 'model':
            observed = item.get('observed', {})
            if not isinstance(observed, dict) or type(observed.get('model_calls')) is not int or observed['model_calls'] != len(CASES):
                raise ValueError('Invalid model call measurements')
            fresh['observed']['model_calls'] = observed['model_calls']
            if 'model' in item:
                model = item['model']
                if not isinstance(model, dict) or set(model) != {'name', 'model_id', 'backend'} or any(not isinstance(v, str) or not 0 < len(v) <= 256 or any(ord(c) < 32 for c in v) for v in model.values()):
                    raise ValueError('Invalid evaluation model identity')
                fresh['model'] = dict(model)
            missing = []
            for key in ('total_tokens', 'cost_usd'):
                value = observed.get(key)
                if value is None:
                    missing.append(key)
                elif type(value) not in ((int,) if key == 'total_tokens' else (int, float)) or not math.isfinite(value) or not 0 <= value <= 1_000_000_000:
                    raise ValueError('Invalid model resource measurements')
                if key == 'total_tokens' and value is not None and value < len(CASES):
                    raise ValueError('Invalid provider token total')
                fresh['observed'][key] = value
            if item.get('missing_metrics') != missing:
                raise ValueError('Model missing measurements mismatch')
            fresh['missing_metrics'] = missing
            fresh['limitation'] = 'Small synthetic suite; provider-reported tokens and configured pricing do not establish safety or an invoice.'
        return fresh

    def record_model(self, report):
        # Internal trusted runner only. No HTTP route accepts reports or metrics.
        if not isinstance(report, dict) or report.get('mode') != 'model':
            raise ValueError('Expected an internally produced model report')
        return self._save(self._report(report))

    def run(self, **kwargs):
        return self._save(run_offline(**kwargs))

    def _save(self, report):
        with _STORE_LOCK:
            history = (self.history() + [report])[-MAX_HISTORY:]
            encoded = json.dumps(history, allow_nan=False)
            if len(encoded.encode()) > MAX_HISTORY_BYTES:
                raise ValueError("Evaluation history size limit reached")
            path = state_path(self.root, self.relative)
            path.parent.chmod(0o700)
            temporary = path.with_name(uuid.uuid4().hex + ".tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return report

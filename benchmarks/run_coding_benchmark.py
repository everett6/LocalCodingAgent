"""Run repeatable, test-verified coding tasks against configured models.

Each attempt gets a fresh workspace, and results are scored in a separate
workspace containing the trusted tests and only the expected implementation
outputs. Multiple configurations can be compared on the same task set.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from local_coder.orchestrator.config_loader import load_config
from local_coder.models.router import resolve_model_name
from local_coder.types import AgentRole
from local_coder.verification.test_runner import TestReport, run_tests


def _relative_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Benchmark file path must stay inside its workspace: {raw_path!r}")
    return path


def _workspace_path(root: Path, raw_path: str) -> Path:
    workspace = root.resolve()
    path = workspace / _relative_path(raw_path)
    try:
        path.resolve().relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"Benchmark path resolves outside its workspace: {raw_path!r}") from exc
    return path


def _write_files(root: Path, files: dict[str, str]) -> None:
    for raw_path, content in files.items():
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise ValueError("Benchmark files must map relative paths to text content")
        path = _workspace_path(root, raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _snapshot_files(root: Path, file_paths: list[str]) -> dict[str, bytes | None]:
    snapshot = {}
    for raw_path in file_paths:
        path = _workspace_path(root, raw_path)
        snapshot[raw_path] = path.read_bytes() if path.is_file() else None
    return snapshot


def _changed_files(root: Path, snapshot: dict[str, bytes | None]) -> list[str]:
    changed = []
    for raw_path, before in snapshot.items():
        path = _workspace_path(root, raw_path)
        after = path.read_bytes() if path.is_file() else None
        if before != after:
            changed.append(raw_path)
    return changed


def _baseline_is_valid(report: TestReport) -> bool:
    return report.parsed and report.failed > 0 and report.errors == 0 and not report.ok


def _agent_claimed_tests_passed(agent_output: str) -> bool | None:
    match = re.search(r"(?im)^Verification:\s*(passed|failed)\s*$", agent_output)
    if not match:
        return None
    return match.group(1).lower() == "passed"


def _summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [item["latency_seconds"] for item in results if item["latency_seconds"] is not None]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(result["task_id"], []).append(result)
    tasks = {}
    for task_id, attempts in grouped.items():
        latencies = [
            item["latency_seconds"]
            for item in attempts
            if item["latency_seconds"] is not None
        ]
        tasks[task_id] = {
            "attempt_count": len(attempts),
            "success_count": sum(item["success"] for item in attempts),
            "success_rate": round(
                sum(item["success"] for item in attempts) / len(attempts), 4
            ),
            "median_latency_seconds": round(statistics.median(latencies), 3) if latencies else None,
        }
    return {
        "attempt_count": len(results),
        "measured_attempt_count": len(measured),
        "success_count": sum(item["success"] for item in results),
        "success_rate": round(sum(item["success"] for item in results) / len(results), 4),
        "latency_seconds": {
            "mean": round(statistics.mean(measured), 3) if measured else None,
            "median": round(statistics.median(measured), 3) if measured else None,
        },
        "tasks": tasks,
    }


def _load_tasks(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or not isinstance(manifest.get("name"), str)
        or not manifest["name"]
        or not isinstance(manifest.get("tasks"), list)
        or not manifest["tasks"]
    ):
        raise ValueError(f"{path}: expected a version 1 manifest with at least one task")
    seen_ids = set()
    for task in manifest["tasks"]:
        if not isinstance(task, dict) or not all(
            isinstance(task.get(key), str) and task[key]
            for key in ("id", "request", "test_file")
        ):
            raise ValueError(f"{path}: each task needs non-empty id, request, and test_file fields")
        if task["id"] in seen_ids:
            raise ValueError(f"{path}: duplicate task id {task['id']!r}")
        seen_ids.add(task["id"])
        if not isinstance(task.get("files"), dict) or not task["files"]:
            raise ValueError(f"{path}: task {task['id']!r} needs starter and test files")
        expected = task.get("expected_changed_files")
        if not isinstance(expected, list) or not expected or not all(isinstance(item, str) for item in expected):
            raise ValueError(f"{path}: task {task['id']!r} needs expected_changed_files")
        if task["test_file"] in expected:
            raise ValueError(f"{path}: task {task['id']!r} cannot treat its trusted test as an output file")
        for raw_path in (*task["files"].keys(), task["test_file"], *expected):
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError(f"{path}: task {task['id']!r} contains an invalid file path")
            _relative_path(raw_path)
        if task["test_file"] not in task["files"]:
            raise ValueError(f"{path}: task {task['id']!r} must include its trusted test file in files")
    return manifest


async def _run_case(
    task: dict[str, Any],
    config_path: Path,
    run_number: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"local-coder-bench-{task['id']}-") as temp_dir:
        workspace = Path(temp_dir) / "workspace"
        workspace.mkdir()
        _write_files(workspace, task["files"])

        baseline = await run_tests(
            workspace, framework="pytest", target=task["test_file"], timeout=120
        )
        if not _baseline_is_valid(baseline):
            raise ValueError(
                f"Task {task['id']!r} has an invalid baseline: expected parsed failing tests, "
                f"got parsed={baseline.parsed}, total={baseline.total}, ok={baseline.ok}. "
                "Check that the test suite fails before the requested change."
            )

        config = load_config(str(config_path), project_root=str(workspace))
        config.project_root = str(workspace)
        config.state_dir = str(workspace / ".local-coder" / "state")
        config.log_dir = str(workspace / ".local-coder" / "logs")
        model_key = resolve_model_name(config, AgentRole.CODER)
        model_config = config.models[model_key]
        from local_coder.orchestrator.coordinator import Coordinator

        coordinator = Coordinator(config=config, project_root=str(workspace))
        observed_events = []
        coordinator.on_event(observed_events.append)
        tracked_files = sorted(set(task["files"]) | set(task["expected_changed_files"]))
        before = _snapshot_files(workspace, tracked_files)
        started = time.perf_counter()
        agent_error = None
        agent_output = ""
        try:
            agent_output = await asyncio.wait_for(
                coordinator.run(task["request"]), timeout=timeout_seconds
            )
        except Exception as exc:
            agent_error = f"{type(exc).__name__}: {exc}"
        finally:
            await coordinator.model_manager.close_all()
        elapsed_seconds = time.perf_counter() - started

        changed_files = _changed_files(workspace, before)
        test_path = _workspace_path(workspace, task["test_file"])
        trusted_tests_unchanged = (
            test_path.is_file()
            and test_path.read_bytes() == task["files"][task["test_file"]].encode("utf-8")
        )
        validation_workspace = Path(temp_dir) / "validation"
        validation_workspace.mkdir()
        _write_files(validation_workspace, task["files"])
        changed_expected_files = sorted(set(changed_files) & set(task["expected_changed_files"]))
        for raw_path in task["expected_changed_files"]:
            generated_path = _workspace_path(workspace, raw_path)
            if generated_path.is_file():
                _write_files(
                    validation_workspace,
                    {raw_path: generated_path.read_text(encoding="utf-8")},
                )
        validation = await run_tests(
            validation_workspace,
            framework="pytest",
            target=task["test_file"],
            timeout=120,
        )
        tests_passed = validation.parsed and validation.total > 0 and validation.ok
        required_files_changed = set(changed_expected_files) == set(task["expected_changed_files"])
        success = (
            agent_error is None
            and tests_passed
            and required_files_changed
            and trusted_tests_unchanged
        )
        return {
            "task_id": task["id"],
            "run": run_number,
            "success": success,
            "model": {
                "config_key": model_key,
                "model_id": model_config.model_id,
                "backend": model_config.backend.value,
            },
            "latency_seconds": round(elapsed_seconds, 3),
            "agent_error": agent_error,
            "agent_claimed_tests_passed": _agent_claimed_tests_passed(agent_output),
            "agent_output_length": len(agent_output),
            "task_completed_events": sum(
                event.event_type == "task_completed" for event in observed_events
            ),
            "changed_files": changed_files,
            "expected_files_changed": required_files_changed,
            "trusted_tests_unchanged": trusted_tests_unchanged,
            "baseline": {
                "parsed": baseline.parsed,
                "passed": baseline.passed,
                "failed": baseline.failed,
                "errors": baseline.errors,
                "ok": baseline.ok,
            },
            "validation": {
                "framework": validation.framework,
                "parsed": validation.parsed,
                "passed": validation.passed,
                "failed": validation.failed,
                "errors": validation.errors,
                "skipped": validation.skipped,
                "ok": validation.ok,
                "summary": validation.raw_output[-2000:],
            },
        }


async def run_benchmark(
    config_path: Path,
    tasks_path: Path,
    runs: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    if runs < 1 or timeout_seconds < 1:
        raise ValueError("runs and timeout_seconds must be positive integers")
    manifest = _load_tasks(tasks_path)
    task_results = []
    for task in manifest["tasks"]:
        for run_number in range(1, runs + 1):
            try:
                task_results.append(
                    await _run_case(task, config_path, run_number, timeout_seconds)
                )
            except Exception as exc:
                task_results.append({
                    "task_id": task["id"],
                    "run": run_number,
                    "success": False,
                    "latency_seconds": None,
                    "benchmark_error": f"{type(exc).__name__}: {exc}",
                })

    summary = _summarize_results(task_results)
    return {
        "schema_version": 1,
        "kind": "test_verified_coding_benchmark",
        "benchmark": manifest["name"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": str(config_path),
        "task_count": len(manifest["tasks"]),
        "runs_per_task": runs,
        **summary,
        "results": task_results,
    }


async def run_comparison(
    config_paths: list[Path],
    tasks_path: Path,
    runs: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    if len(config_paths) < 2:
        raise ValueError("A model comparison needs a baseline config and at least one candidate config")
    if len({path.resolve() for path in config_paths}) != len(config_paths):
        raise ValueError("Each comparison config must be unique")

    reports = [
        await run_benchmark(path.resolve(), tasks_path, runs, timeout_seconds)
        for path in config_paths
    ]
    baseline = reports[0]
    comparisons = []
    for candidate in reports[1:]:
        baseline_tasks = baseline["tasks"]
        task_deltas = {}
        for task_id, candidate_task in candidate["tasks"].items():
            baseline_rate = baseline_tasks[task_id]["success_rate"]
            task_deltas[task_id] = round(candidate_task["success_rate"] - baseline_rate, 4)
        baseline_median = baseline["latency_seconds"]["median"]
        candidate_median = candidate["latency_seconds"]["median"]
        comparisons.append({
            "baseline_config": baseline["config"],
            "candidate_config": candidate["config"],
            "success_rate_delta": round(candidate["success_rate"] - baseline["success_rate"], 4),
            "median_latency_ratio": (
                round(candidate_median / baseline_median, 3)
                if baseline_median is not None
                and baseline_median > 0
                and candidate_median is not None
                else None
            ),
            "task_success_rate_deltas": task_deltas,
        })

    return {
        "schema_version": 1,
        "kind": "test_verified_coding_model_comparison",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "benchmark": baseline["benchmark"],
        "runs_per_task": runs,
        "baseline": baseline,
        "candidates": reports[1:],
        "comparisons": comparisons,
        "exit_error_count": sum(
            "benchmark_error" in result
            for report in reports
            for result in report["results"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=REPOSITORY_ROOT / "config" / "config.yaml")
    parser.add_argument(
        "--compare",
        type=Path,
        action="append",
        default=[],
        metavar="CONFIG",
        help="Compare another model config against --config; repeat for more candidates",
    )
    parser.add_argument("--tasks", type=Path, default=REPOSITORY_ROOT / "benchmarks" / "coding_tasks.json")
    parser.add_argument("--runs", type=int, default=1, help="Fresh-workspace repetitions per task")
    parser.add_argument("--timeout", type=int, default=900, help="Maximum seconds per agent run")
    parser.add_argument(
        "--output", type=Path, default=REPOSITORY_ROOT / "benchmark_task_results.json"
    )
    args = parser.parse_args()
    if args.runs < 1 or args.timeout < 1:
        parser.error("--runs and --timeout must be positive integers")
    if not args.config.is_file():
        parser.error(f"Config file not found: {args.config}")
    if not args.tasks.is_file():
        parser.error(f"Task manifest not found: {args.tasks}")

    if args.compare:
        report = asyncio.run(
            run_comparison(
                [args.config, *args.compare],
                args.tasks.resolve(),
                args.runs,
                args.timeout,
            )
        )
        exit_error_count = report["exit_error_count"]
    else:
        report = asyncio.run(
            run_benchmark(args.config.resolve(), args.tasks.resolve(), args.runs, args.timeout)
        )
        exit_error_count = sum(
            "benchmark_error" in result for result in report["results"]
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nReport saved to {args.output}")
    return 1 if exit_error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())

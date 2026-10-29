import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

import pytest

from benchmarks.run_coding_benchmark import (
    _agent_claimed_tests_passed,
    _baseline_is_valid,
    _changed_files,
    _load_tasks,
    _relative_path,
    _snapshot_files,
    _write_files,
    _run_case,
    run_benchmark,
    run_comparison,
)
from local_coder.verification.test_runner import run_tests


def test_benchmark_rejects_workspace_escape_paths():
    with pytest.raises(ValueError, match="stay inside"):
        _relative_path("../outside.py")

    with pytest.raises(ValueError, match="stay inside"):
        _relative_path("/tmp/outside.py")


def test_benchmark_rejects_symlinked_paths_outside_workspace(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside its workspace"):
        _write_files(workspace, {"escape/file.py": "must not be written"})

    assert not (outside / "file.py").exists()


def test_benchmark_tracks_changed_files(tmp_path):
    _write_files(tmp_path, {"src/module.py": "original\n"})
    before = _snapshot_files(tmp_path, ["src/module.py", "src/new.py"])
    (tmp_path / "src" / "module.py").write_text("updated\n", encoding="utf-8")
    (tmp_path / "src" / "new.py").write_text("new\n", encoding="utf-8")

    assert _changed_files(tmp_path, before) == ["src/module.py", "src/new.py"]


def test_baseline_requires_a_parsed_failing_test():
    assert _baseline_is_valid(SimpleNamespace(parsed=True, failed=1, errors=0, total=1, ok=False))
    assert not _baseline_is_valid(SimpleNamespace(parsed=False, total=1, ok=False))
    assert not _baseline_is_valid(
        SimpleNamespace(parsed=True, failed=0, errors=0, total=0, ok=False)
    )
    assert not _baseline_is_valid(
        SimpleNamespace(parsed=True, failed=0, errors=0, total=1, ok=True)
    )
    assert not _baseline_is_valid(SimpleNamespace(parsed=True, failed=0, errors=1, total=1, ok=False))


def test_agent_verification_claim_is_reported_separately_from_test_result():
    assert _agent_claimed_tests_passed("Verification: passed\n") is True
    assert _agent_claimed_tests_passed("Verification: failed\n") is False
    assert _agent_claimed_tests_passed("The change is complete.") is None


def test_task_manifest_validates_ids_and_test_file(tmp_path):
    path = tmp_path / "tasks.json"
    manifest = {
        "version": 1,
        "name": "sample",
        "tasks": [{
            "id": "one",
            "request": "Implement it",
            "files": {"app.py": "pass\n", "tests/test_app.py": "def test_ok(): pass\n"},
            "test_file": "tests/test_app.py",
            "expected_changed_files": ["app.py"],
        }],
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert _load_tasks(path)["tasks"][0]["id"] == "one"

    manifest["tasks"][0]["test_file"] = "tests/missing.py"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="include its trusted test file"):
        _load_tasks(path)

    manifest["tasks"][0]["test_file"] = "tests/test_app.py"
    manifest["tasks"][0]["expected_changed_files"] = ["tests/test_app.py"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="trusted test"):
        _load_tasks(path)


def test_coding_benchmark_fixture_starts_with_a_real_failing_test(tmp_path):
    tasks = _load_tasks(Path(__file__).parents[1] / "benchmarks" / "coding_tasks.json")["tasks"]
    for task in tasks:
        workspace = tmp_path / task["id"]
        workspace.mkdir()
        _write_files(workspace, task["files"])

        report = asyncio.run(
            run_tests(workspace, framework="pytest", target=task["test_file"], timeout=30)
        )

        assert _baseline_is_valid(report), f"{task['id']}: {report.raw_output}"


def test_benchmark_records_setup_errors_as_failed_attempts(tmp_path):
    task_path = Path(__file__).parents[1] / "benchmarks" / "coding_tasks.json"

    report = asyncio.run(
        run_benchmark(
            tmp_path / "missing-config.yaml",
            task_path,
            runs=1,
            timeout_seconds=1,
        )
    )

    assert report["success_rate"] == 0
    assert report["latency_seconds"] == {"mean": None, "median": None}
    assert "Config file not found" in report["results"][0]["benchmark_error"]


def test_result_summary_reports_success_and_latency_per_task():
    from benchmarks.run_coding_benchmark import _summarize_results

    summary = _summarize_results([
        {"task_id": "alpha", "success": True, "latency_seconds": 2.0},
        {"task_id": "alpha", "success": False, "latency_seconds": 4.0},
        {"task_id": "beta", "success": True, "latency_seconds": None},
    ])

    assert summary["success_rate"] == pytest.approx(2 / 3, abs=0.0001)
    assert summary["latency_seconds"] == {"mean": 3.0, "median": 3.0}
    assert summary["tasks"]["alpha"]["success_rate"] == 0.5
    assert summary["tasks"]["alpha"]["median_latency_seconds"] == 3.0
    assert summary["tasks"]["beta"]["median_latency_seconds"] is None


def test_model_comparison_reports_success_and_latency_deltas(tmp_path, monkeypatch):
    async def fake_run_benchmark(config_path, tasks_path, runs, timeout_seconds):
        baseline = config_path.name == "baseline.yaml"
        return {
            "schema_version": 1,
            "kind": "test_verified_coding_benchmark",
            "timestamp": "test",
            "benchmark": "sample",
            "config": str(config_path),
            "runs_per_task": runs,
            "attempt_count": 2,
            "measured_attempt_count": 2,
            "success_count": 1 if baseline else 2,
            "success_rate": 0.5 if baseline else 1.0,
            "latency_seconds": {"mean": 3.0 if baseline else 2.0, "median": 3.0 if baseline else 2.0},
            "tasks": {
                "alpha": {
                    "attempt_count": 2,
                    "success_count": 1 if baseline else 2,
                    "success_rate": 0.5 if baseline else 1.0,
                    "median_latency_seconds": 3.0 if baseline else 2.0,
                }
            },
            "results": [],
        }

    monkeypatch.setattr("benchmarks.run_coding_benchmark.run_benchmark", fake_run_benchmark)
    baseline_path = tmp_path / "baseline.yaml"
    candidate_path = tmp_path / "candidate.yaml"
    report = asyncio.run(
        run_comparison([baseline_path, candidate_path], tmp_path / "tasks.json", 2, 60)
    )

    comparison = report["comparisons"][0]
    assert comparison["success_rate_delta"] == 0.5
    assert comparison["median_latency_ratio"] == pytest.approx(2 / 3, abs=0.001)
    assert comparison["task_success_rate_deltas"] == {"alpha": 0.5}


@pytest.mark.parametrize("tamper_tests", [False, True])
def test_case_scores_changes_against_isolated_trusted_tests(tmp_path, monkeypatch, tamper_tests):
    task = _load_tasks(Path(__file__).parents[1] / "benchmarks" / "coding_tasks.json")["tasks"][0]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  coder:\n"
        "    backend: openai_compatible\n"
        "    model_id: fake-model\n"
        "    base_url: http://127.0.0.1:1/v1\n"
        "agentic:\n"
        "  role_models:\n"
        "    coder: coder\n",
        encoding="utf-8",
    )

    class FakeCoordinator:
        def __init__(self, config, project_root):
            self.project_root = Path(project_root)
            self.model_manager = SimpleNamespace(close_all=self._close_models)
            self.handlers = []

        async def _close_models(self):
            return None

        def on_event(self, handler):
            self.handlers.append(handler)

        async def run(self, request):
            (self.project_root / "math_utils.py").write_text(
                "def normalize_whitespace(value: str) -> str:\n"
                "    return ' '.join(value.split())\n",
                encoding="utf-8",
            )
            if tamper_tests:
                (self.project_root / "tests" / "test_math_utils.py").write_text(
                    "def test_agent_cannot_replace_the_benchmark():\n"
                    "    assert False\n",
                    encoding="utf-8",
                )
            return "Final Report\n\nVerification: passed"

    coordinator_module = ModuleType("local_coder.orchestrator.coordinator")
    setattr(coordinator_module, "Coordinator", FakeCoordinator)
    monkeypatch.setitem(sys.modules, "local_coder.orchestrator.coordinator", coordinator_module)

    result = asyncio.run(_run_case(task, config_path, 1, timeout_seconds=5))

    assert result["success"] is not tamper_tests
    assert result["agent_claimed_tests_passed"] is True
    assert result["baseline"]["failed"] > 0
    assert result["validation"]["ok"] is True
    assert result["trusted_tests_unchanged"] is not tamper_tests
    assert result["changed_files"] == (
        ["math_utils.py", "tests/test_math_utils.py"] if tamper_tests else ["math_utils.py"]
    )

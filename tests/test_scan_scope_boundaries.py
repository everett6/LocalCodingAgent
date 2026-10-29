"""Local scan enumeration and reads reject special files and disclose limits."""
import asyncio
import os
from pathlib import Path

import pytest

from local_coder.context.source_snapshot import SourceSnapshotError
from local_coder.tools import scan_scope
from local_coder.tools.bugs import RunBugScanTool


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_enumeration_rejects_fifos_as_sources_and_direct_targets(tmp_path):
    os.mkfifo(tmp_path / "pipe.py")
    (tmp_path / "source.py").write_text("value = 1\n")
    files, coverage = scan_scope.scan_files(tmp_path, tmp_path)
    assert [path.name for path in files] == ["source.py"]
    assert coverage["status"] == "partial"
    assert coverage["skipped_count"] == 1
    direct, coverage = scan_scope.scan_files(tmp_path, tmp_path / "pipe.py")
    assert direct == [] and coverage["status"] == "partial"
    with pytest.raises(SourceSnapshotError, match="regular"):
        scan_scope.read_scan_source(tmp_path, tmp_path / "pipe.py")


def test_scan_source_uses_bounded_snapshots_and_can_inspect_credential_fixtures(tmp_path, monkeypatch):
    fixture = tmp_path / ".env.fixture"
    fixture.write_text("PASSWORD=synthetic-fixture-only\n")
    def unbounded(*args, **kwargs):
        pytest.fail("Unbounded Path read used for scanner source")
    monkeypatch.setattr(Path, "read_text", unbounded)
    monkeypatch.setattr(Path, "read_bytes", unbounded)
    coverage = {}
    assert scan_scope.read_scan_source(tmp_path, fixture, coverage=coverage).startswith("PASSWORD=")
    assert coverage["bytes_read"] == fixture.stat().st_size


def test_actual_read_budget_limits_growth_after_preflight(tmp_path, monkeypatch):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_bytes(b"x" * 8)
    second.write_bytes(b"y" * 2)
    files, coverage = scan_scope.scan_files(tmp_path, tmp_path)
    assert len(files) == 2
    monkeypatch.setattr(scan_scope, "MAX_SCAN_BYTES", 10)
    assert len(scan_scope.read_scan_source(tmp_path, first, coverage=coverage)) == 8
    second.write_bytes(b"y" * 4)
    with pytest.raises(SourceSnapshotError, match="byte limit"):
        scan_scope.read_scan_source(tmp_path, second, coverage=coverage)
    assert coverage["bytes_read"] == 8
    assert coverage["read_budget_used"] == 10


def test_failed_reads_cannot_repeatedly_escape_the_aggregate_byte_budget(tmp_path, monkeypatch):
    calls = []
    def changed(*args, **kwargs):
        calls.append(kwargs["max_bytes"])
        raise SourceSnapshotError("Source changed while reading")
    monkeypatch.setattr(scan_scope, "MAX_SCAN_BYTES", 8)
    monkeypatch.setattr(scan_scope, "MAX_SCAN_FILE_BYTES", 4)
    monkeypatch.setattr(scan_scope, "read_source_snapshot", changed)
    coverage = {}
    for _ in range(3):
        with pytest.raises(SourceSnapshotError):
            scan_scope.read_scan_source(tmp_path, "source.py", coverage=coverage)
    assert calls == [4, 4]
    assert coverage.get("bytes_read", 0) == 0
    assert coverage["read_bytes_reserved"] == 8
    assert coverage["read_budget_used"] == 8


def test_source_swap_to_symlink_between_enumeration_and_read_is_rejected(tmp_path):
    source = tmp_path / "source.py"
    actual = tmp_path / "actual.py"
    source.write_text("value = 1\n")
    actual.write_text("value = 2\n")
    files, coverage = scan_scope.scan_files(tmp_path, source)
    source.unlink()
    try:
        source.symlink_to(actual)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises((SourceSnapshotError, OSError)):
        scan_scope.read_scan_source(tmp_path, files[0], coverage=coverage)
    assert coverage["bytes_read"] == 0


@pytest.mark.parametrize("limit_name", ["MAX_SCAN_ENTRIES", "MAX_SCAN_DIRECTORIES", "MAX_SCAN_DEPTH"])
def test_enumeration_budgets_report_partial_coverage(tmp_path, monkeypatch, limit_name):
    for index in range(4):
        folder = tmp_path / f"part-{index}" / "nested"
        folder.mkdir(parents=True)
        (folder / "source.py").write_text("value = 1\n")
    monkeypatch.setattr(scan_scope, limit_name, 1)
    files, coverage = scan_scope.scan_files(tmp_path, tmp_path)
    assert coverage["status"] == "partial"
    assert coverage["skipped"]
    assert "budget" in str(coverage["skipped"]).lower() or "depth" in str(coverage["skipped"]).lower()
    assert coverage["entries_considered"] <= scan_scope.MAX_SCAN_ENTRIES
    assert coverage["directories_considered"] <= scan_scope.MAX_SCAN_DIRECTORIES


def test_skip_diagnostics_are_bounded_and_preserve_omitted_count():
    coverage = {}
    for index in range(scan_scope.MAX_SKIP_RECORDS + 5):
        scan_scope.record_scan_skip(coverage, f"source-{index}.py", "fixture limit")
    assert len(coverage["skipped"]) == scan_scope.MAX_SKIP_RECORDS
    assert coverage["skipped_count"] == scan_scope.MAX_SKIP_RECORDS + 5
    assert coverage["skipped_omitted"] == 5
    assert coverage["status"] == "partial"


def test_directory_replaced_by_symlink_before_open_is_not_traversed(tmp_path, monkeypatch):
    from contextlib import contextmanager
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "original.py").write_text("value = 1\n")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "alias-only.py").write_text("value = 2\n")
    original = scan_scope._directory_entries
    @contextmanager
    def replace_before_open(root, directory):
        if directory == source_dir:
            source_dir.rename(tmp_path / "held-source")
            try:
                source_dir.symlink_to(replacement, target_is_directory=True)
            except OSError:
                pytest.skip("symlinks unavailable")
        with original(root, directory) as entries:
            yield entries
    monkeypatch.setattr(scan_scope, "_directory_entries", replace_before_open)
    files, coverage = scan_scope.scan_files(tmp_path, tmp_path)
    assert not any(path.parent == source_dir for path in files)
    assert coverage["status"] == "partial"
    assert "linked directory" in str(coverage["skipped"])


def test_directory_read_failure_cannot_be_reported_as_complete(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise PermissionError("fixture directory failure")
    monkeypatch.setattr(scan_scope, "_directory_entries", unavailable)
    files, coverage = scan_scope.scan_files(tmp_path, tmp_path)
    assert files == []
    assert coverage["status"] == "partial"
    assert coverage["skipped"]


def test_bug_scan_uses_snapshot_reads_and_reports_unsupported_input(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_text("def collect(items=[]):\n    return items\n")
    (tmp_path / "source.rs").write_text("fn main() {}")
    original = Path.read_text
    def reject_source(path, *args, **kwargs):
        if path.suffix == ".py":
            pytest.fail("Bug scanner used an unbounded source read")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", reject_source)
    result = asyncio.run(RunBugScanTool(str(tmp_path)).execute())
    assert result.success
    assert result.data["findings"][0]["rule_id"] == "BUG-MUTABLE-DEFAULT"
    assert result.data["coverage"]["reviewed"] == ["source.py"]
    assert result.data["coverage"]["status"] == "partial"


def test_nested_async_bodies_are_not_reported_twice(tmp_path):
    (tmp_path / "source.py").write_text("import time\nasync def outer():\n    async def inner():\n        time.sleep(1)\n")
    result = asyncio.run(RunBugScanTool(str(tmp_path)).execute())
    findings = [item for item in result.data["findings"] if item["rule_id"] == "BUG-BLOCKING-ASYNC"]
    assert len(findings) == 1
    assert findings[0]["line"] == 4


def test_bug_ast_limit_is_partial_coverage(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_text("value = 1\n" * 10)
    monkeypatch.setattr("local_coder.tools.bugs.MAX_BUG_AST_NODES", 5)
    result = asyncio.run(RunBugScanTool(str(tmp_path)).execute())
    assert result.success
    assert result.data["coverage"]["status"] == "partial"
    assert "AnalysisLimit" in str(result.data["coverage"]["skipped"])


def test_semantic_guard_trace_expansion_has_a_bounded_observation_budget():
    from local_coder.security.local_analysis import analyze_unit
    from local_coder.security_analysis import AnalysisLimit
    guards = "    if value:\n        pass\n" * 150
    calls = "    service(value)\n" * 100
    source = "@app.get('/')\ndef route(value):\n" + guards + calls + "\ndef service(value):\n" + guards
    assert len(source.encode()) < 256000
    with pytest.raises(AnalysisLimit, match="observations"):
        analyze_unit(source, "fixture.py")


def test_semantic_limit_surfaces_as_partial_sast_coverage(tmp_path, monkeypatch):
    from local_coder.tools.security import RunSASTTool
    (tmp_path / "source.py").write_text("@app.get('/')\ndef route(value):\n    return service(value)\ndef service(value):\n    return value\n")
    monkeypatch.setattr("local_coder.security.local_analysis.MAX_OBSERVATION_BYTES", 100)
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool="builtin"))
    assert result.success
    assert result.data["coverage"]["status"] == "partial"
    assert "AnalysisLimit" in str(result.data["coverage"]["failed"])


@pytest.mark.parametrize("limit_name,value", [
    ("MAX_CANDIDATE_SOURCE_BYTES", 10), ("MAX_CANDIDATE_AST_NODES", 5),
    ("MAX_CANDIDATE_FINDINGS", 1), ("MAX_CANDIDATE_BYTES", 100),
])
def test_python_candidate_limits_fail_explicitly(monkeypatch, limit_name, value):
    from local_coder.tools import detection
    from local_coder.security_analysis import AnalysisLimit
    monkeypatch.setattr(detection, limit_name, value)
    with pytest.raises(AnalysisLimit):
        detection.python_candidates("eval(user)\nexec(user)\n", "fixture.py")


def test_same_line_candidate_expansion_is_bounded_before_appending_many_findings(monkeypatch):
    from local_coder.tools import detection
    from local_coder.security_analysis import AnalysisLimit
    source = ";".join("eval(user)" for _ in range(1500))
    assert len(source.encode()) < detection.MAX_CANDIDATE_SOURCE_BYTES
    with pytest.raises(AnalysisLimit, match="finding bytes"):
        detection.python_candidates(source, "fixture.py")


def test_candidate_limit_preserves_partial_sast_coverage(tmp_path, monkeypatch):
    from local_coder.tools.security import RunSASTTool
    (tmp_path / "source.py").write_text("eval(user)\nexec(user)\n")
    monkeypatch.setattr("local_coder.tools.detection.MAX_CANDIDATE_FINDINGS", 1)
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool="builtin"))
    assert result.success
    assert result.data["coverage"]["status"] == "partial"
    assert "AnalysisLimit" in str(result.data["coverage"]["failed"])

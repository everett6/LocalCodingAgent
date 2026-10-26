import asyncio

from local_coder.tools import create_tool_registry
from local_coder.tools.security import AuditConfigTool, AuditDependenciesTool, RunSASTTool
from local_coder.types import AgentRole, ToolName


def test_security_scans_reject_outside_workspace_and_symlinks(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside.py").write_text("eval(input())")
    (root / "escape.py").symlink_to(tmp_path / "outside.py")
    async def scenario():
        for tool, args in [(RunSASTTool(str(root)), {"target_path": "../outside.py", "tool": "builtin"}),
                           (AuditConfigTool(str(root)), {"path": "../outside.py"})]:
            result = await tool.execute(**args)
            assert not result.success
            assert "workspace" in result.error.lower()
        result = await RunSASTTool(str(root)).execute(tool="builtin")
        assert "outside.py" not in result.output
        assert "escape.py" not in result.output
    asyncio.run(scenario())


def test_sast_structured_findings_and_safe_yaml_loader(tmp_path):
    (tmp_path / "app.py").write_text('import yaml\nyaml.load(data, Loader=yaml.SafeLoader)\neval(data)\n')
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool="builtin"))
    assert result.success
    assert "SEC-PY-YAML-UNSAFE" not in result.output
    assert result.data["findings"][0]["line"] == 3
    assert result.data["findings"][0]["verification"] == "unverified"


def test_missing_scanner_is_not_clean(tmp_path, monkeypatch):
    monkeypatch.setattr("local_coder.tools.security.shutil.which", lambda _: None)
    (tmp_path / "requirements.txt").write_text("requests>=2.32\n")
    result = asyncio.run(AuditDependenciesTool(str(tmp_path)).execute())
    assert "not checked" in result.output.lower()
    assert "clean" not in result.output.lower()
    assert "CVE-" not in result.output


def test_bug_scan_reports_boundaries_without_executing_project(tmp_path):
    (tmp_path / "bug.py").write_text('def collect(items=[]):\n    try:\n        return items\n    except:\n        pass\n')
    from local_coder.tools.bugs import RunBugScanTool
    result = asyncio.run(RunBugScanTool(str(tmp_path)).execute())
    assert result.success
    rules = {f["rule_id"] for f in result.data["findings"]}
    assert {"BUG-MUTABLE-DEFAULT", "BUG-BARE-EXCEPT"} <= rules
    registry = create_tool_registry(str(tmp_path))
    assert registry.has_permission(AgentRole.BUG_HUNTER, ToolName.RUN_BUG_SCAN)


def test_scan_budget_reports_skipped_files(tmp_path):
    (tmp_path / "big.py").write_text("#" * 1_100_000)
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool="builtin"))
    assert result.data["coverage"]["skipped"]
    assert "partial" in result.output.lower()


def test_invalid_python_is_reported_as_partial_coverage(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n")
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool="builtin"))
    assert result.data["coverage"]["status"] == "partial"
    assert "SyntaxError" in str(result.data["coverage"]["skipped"])


def test_concurrent_scans_keep_their_own_target(tmp_path, monkeypatch):
    import threading
    (tmp_path / 'first.py').write_text('eval(value)\n')
    (tmp_path / 'second.py').write_text('exec(value)\n')
    tool = RunSASTTool(str(tmp_path))
    original = tool._run_builtin_sast
    barrier = threading.Barrier(2)
    def simultaneous(*args):
        barrier.wait(timeout=3)
        return original(*args)
    monkeypatch.setattr(tool, '_run_builtin_sast', simultaneous)
    async def scenario():
        first, second = await asyncio.gather(tool.execute('first.py', tool='builtin'), tool.execute('second.py', tool='builtin'))
        assert {finding['file'] for finding in first.data['findings']} == {'first.py'}
        assert {finding['file'] for finding in second.data['findings']} == {'second.py'}
    asyncio.run(scenario())

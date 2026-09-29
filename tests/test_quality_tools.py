"""Tests for the lint and format_code tools."""
import asyncio
import stat

import pytest

from local_coder.tools import create_tool_registry
from local_coder.tools.quality import FormatCodeTool, LintTool, detect_languages
from local_coder.types import AgentRole, ToolName


def _fake_executable(bin_dir, name, script):
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (root / "app.py").write_text("x=1\n")
    return root


def test_detect_languages_follows_markers(tmp_path):
    (tmp_path / "go.mod").write_text("module demo\n")
    (tmp_path / "pyproject.toml").write_text("")

    assert detect_languages(str(tmp_path)) == ["python", "go"]


def test_lint_auto_detects_installed_linter(project, fake_bin):
    _fake_executable(fake_bin, "ruff", 'echo "ran ruff $@"; echo "app.py:1:2: E225 missing whitespace"; exit 1\n')

    result = asyncio.run(LintTool(str(project)).execute())

    assert not result.success
    assert result.output.startswith("$ ruff check .")
    assert "ran ruff check ." in result.output
    assert "E225" in result.output
    assert result.files_changed == []


def test_lint_falls_back_to_next_linter_and_passes_paths(project, fake_bin):
    _fake_executable(fake_bin, "flake8", 'echo "flake8 $@"\n')

    result = asyncio.run(LintTool(str(project)).execute(paths=["app.py"]))

    assert result.success
    assert "flake8 app.py" in result.output


def test_lint_prefers_project_local_node_binary(tmp_path, fake_bin):
    (tmp_path / "package.json").write_text("{}")
    local_bin = tmp_path / "node_modules" / ".bin"
    local_bin.mkdir(parents=True)
    _fake_executable(local_bin, "eslint", 'echo "local eslint $@"\n')

    result = asyncio.run(LintTool(str(tmp_path)).execute())

    assert result.success
    assert "local eslint ." in result.output


def test_lint_reports_missing_linters(project, fake_bin):
    result = asyncio.run(LintTool(str(project)).execute())

    assert not result.success
    assert "Install one of: ruff, flake8" in result.output


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"tool": "rm"}, "Unknown linter 'rm'"),
        ({"tool": "flake8"}, "flake8 is not installed"),
        ({"paths": ["../outside"]}, "escapes workspace"),
    ],
)
def test_lint_rejects_bad_requests(project, fake_bin, kwargs, message):
    _fake_executable(fake_bin, "ruff", "exit 0\n")

    result = asyncio.run(LintTool(str(project)).execute(**kwargs))

    assert not result.success
    assert message in result.output


def test_lint_guards_paths_that_look_like_options(project, fake_bin):
    _fake_executable(fake_bin, "ruff", 'echo "args: $@"\n')
    (project / "--fix").write_text("")

    result = asyncio.run(LintTool(str(project)).execute(paths=["--fix"]))

    assert "args: check ./--fix" in result.output


def test_format_reports_rewritten_files(project, fake_bin):
    (project / "clean.py").write_text("y = 2\n")
    _fake_executable(fake_bin, "ruff", 'printf "x = 1\\n" > app.py\n')

    result = asyncio.run(FormatCodeTool(str(project)).execute())

    assert result.success, result.output
    assert result.files_changed == ["app.py"]
    assert "Formatted 1 file(s): app.py" in result.output
    assert (project / "app.py").read_text() == "x = 1\n"


def test_format_reports_no_changes(project, fake_bin):
    _fake_executable(fake_bin, "black", "exit 0\n")

    result = asyncio.run(FormatCodeTool(str(project)).execute(tool="black"))

    assert result.success
    assert result.files_changed == []
    assert "No files changed." in result.output


def test_format_timeout_is_reported(project, fake_bin):
    _fake_executable(fake_bin, "ruff", "/bin/sleep 5\n")

    result = asyncio.run(FormatCodeTool(str(project), timeout=1).execute())

    assert not result.success
    assert "timed out after 1 seconds" in result.output


def test_quality_tool_permissions(tmp_path):
    registry = create_tool_registry(str(tmp_path))

    for role in (AgentRole.CODER, AgentRole.DEBUGGER, AgentRole.TESTER, AgentRole.REVIEWER, AgentRole.SECURITY):
        assert registry.has_permission(role, ToolName.LINT)
    for role in (AgentRole.CODER, AgentRole.DEBUGGER, AgentRole.TESTER):
        assert registry.has_permission(role, ToolName.FORMAT_CODE)
    # Review roles must stay read-only.
    for role in (AgentRole.REVIEWER, AgentRole.SECURITY, AgentRole.EXPLORER, AgentRole.PLANNER):
        assert not registry.has_permission(role, ToolName.FORMAT_CODE)

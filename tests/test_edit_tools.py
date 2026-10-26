"""Tests for the edit_file tool and apply_patch change reporting."""
import asyncio
import shutil

import pytest

from local_coder.tools import create_tool_registry
from local_coder.tools.filesystem import ApplyPatchTool, EditFileTool, _patched_files
from local_coder.types import AgentRole, ToolName


def _edit(tmp_path, **kwargs):
    return asyncio.run(EditFileTool(str(tmp_path)).execute(**kwargs))


def test_edit_file_replaces_unique_snippet(tmp_path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n")

    result = _edit(tmp_path, path="app.py", old_string="return a - b", new_string="return a + b")

    assert result.success
    assert result.files_changed == ["app.py"]
    assert "line 2" in result.output
    assert (tmp_path / "app.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_edit_file_rejects_ambiguous_match_and_lists_lines(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\ny = 2\nx = 1\n")

    result = _edit(tmp_path, path="app.py", old_string="x = 1", new_string="x = 3")

    assert not result.success
    assert "2 times" in result.output
    assert "lines 1, 3" in result.output
    assert (tmp_path / "app.py").read_text() == "x = 1\ny = 2\nx = 1\n"


def test_edit_file_replace_all(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\ny = 2\nx = 1\n")

    result = _edit(tmp_path, path="app.py", old_string="x = 1", new_string="x = 3", replace_all=True)

    assert result.success
    assert "2 occurrences" in result.output
    assert (tmp_path / "app.py").read_text() == "x = 3\ny = 2\nx = 3\n"


def test_edit_file_points_at_whitespace_near_miss(tmp_path):
    (tmp_path / "app.py").write_text("class A:\n    def f(self):\n        return 1\n")

    result = _edit(
        tmp_path,
        path="app.py",
        old_string="def f(self):\n    return 1",
        new_string="def f(self):\n    return 2",
    )

    assert not result.success
    assert "whitespace-insensitive match starts at line 2" in result.output


def test_edit_file_reports_missing_text(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")

    result = _edit(tmp_path, path="app.py", old_string="y = 2", new_string="y = 3")

    assert not result.success
    assert "not found" in result.output


def test_edit_file_preserves_crlf_line_endings(tmp_path):
    (tmp_path / "app.py").write_bytes(b"a = 1\r\nb = 2\r\n")

    result = _edit(tmp_path, path="app.py", old_string="a = 1\nb = 2", new_string="a = 1\nb = 3")

    assert result.success
    assert (tmp_path / "app.py").read_bytes() == b"a = 1\r\nb = 3\r\n"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"path": "missing.py", "old_string": "a", "new_string": "b"}, "File not found"),
        ({"path": "app.py", "old_string": "", "new_string": "b"}, "must not be empty"),
        ({"path": "app.py", "old_string": "a", "new_string": "a"}, "identical"),
        ({"path": "../outside.py", "old_string": "a", "new_string": "b"}, "escapes workspace"),
    ],
)
def test_edit_file_rejects_invalid_requests(tmp_path, kwargs, message):
    (tmp_path / "app.py").write_text("a\n")

    result = _edit(tmp_path, **kwargs)

    assert not result.success
    assert message in result.output
    assert (tmp_path / "app.py").read_text() == "a\n"


def test_edit_file_is_available_to_editing_roles_only(tmp_path):
    registry = create_tool_registry(str(tmp_path))

    for role in (AgentRole.CODER, AgentRole.DEBUGGER, AgentRole.TESTER):
        assert registry.has_permission(role, ToolName.EDIT_FILE)
    for role in (AgentRole.EXPLORER, AgentRole.PLANNER, AgentRole.REVIEWER):
        assert not registry.has_permission(role, ToolName.EDIT_FILE)
    assert registry.get_tool(ToolName.EDIT_FILE) is not None


def test_patched_files_parses_headers():
    patch = (
        "--- a/src/app.py\t2024-01-01\n+++ b/src/app.py\t2024-01-02\n@@ -1 +1 @@\n-x\n+y\n"
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+z\n"
        "--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-w\n"
    )

    assert _patched_files(patch) == ["src/app.py", "new.py", "old.py"]


@pytest.mark.skipif(shutil.which("patch") is None, reason="patch binary not installed")
def test_apply_patch_reports_files_changed(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text("x = 1\n")
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"

    result = asyncio.run(ApplyPatchTool(str(tmp_path)).execute(patch=patch, working_dir="pkg"))

    assert result.success, result.output
    assert result.files_changed == ["pkg/app.py"]
    assert (tmp_path / "pkg" / "app.py").read_text() == "x = 2\n"

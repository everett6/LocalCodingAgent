"""Tool outcomes must prevent false empty searches and repeated mutations."""
import asyncio
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from local_coder.tools.base import ToolRegistry
from local_coder.tools.filesystem import EditFileTool
from local_coder.tools.search import GrepTool
from local_coder.types import AgentRole


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("file_pattern", [None, "*.txt"])
def test_grep_invalid_regex_is_failure(tmp_path, monkeypatch, fallback, file_pattern):
    if fallback:
        monkeypatch.setattr("local_coder.tools.search.shutil.which", lambda name: None)
    elif shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    (tmp_path / "source.txt").write_text("needle\n")

    result = asyncio.run(GrepTool(str(tmp_path)).execute("[", file_pattern=file_pattern))

    assert not result.success
    assert "No matches" not in result.output
    assert result.output.strip()


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("file_pattern", [None, "*.txt"])
def test_grep_option_looking_pattern_is_literal(tmp_path, monkeypatch, fallback, file_pattern):
    if fallback:
        monkeypatch.setattr("local_coder.tools.search.shutil.which", lambda name: None)
    elif shutil.which("rg") is None:
        pytest.skip("ripgrep unavailable")
    (tmp_path / "source.txt").write_text("--local-marker\n")

    result = asyncio.run(GrepTool(str(tmp_path)).execute("--local-marker", file_pattern=file_pattern))

    assert result.success
    assert "source.txt:1:--local-marker" in result.output


@pytest.mark.parametrize("failure", [OSError, ValueError, sqlite3.OperationalError])
def test_successful_edit_survives_memory_write_failure(tmp_path, failure):
    (tmp_path / "counter.txt").write_text("counter = 1\n")
    registry = ToolRegistry()
    registry.register(EditFileTool(str(tmp_path)))

    def unavailable_memory(*args, **kwargs):
        raise failure("memory storage unavailable")

    registry.context_memory = SimpleNamespace(ingest=unavailable_memory)
    result = asyncio.run(registry.execute_tool(AgentRole.CODER, "edit_file", {
        "path": "counter.txt", "old_string": "1", "new_string": "11",
    }))

    assert (tmp_path / "counter.txt").read_text() == "counter = 11\n"
    assert result.success
    assert result.files_changed == ["counter.txt"]
    assert result.data.get("memory_ingest_failed") is True
    assert not result.data.get("mutation_completion_unknown")

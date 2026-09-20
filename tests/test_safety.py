"""Tests for workspace boundaries and command safety."""
import asyncio

import pytest

from local_coder.safety import CommandPolicy, CommandRisk
from local_coder.tools.shell import RunCommandTool
from local_coder.workspace import Workspace


def test_workspace_rejects_parent_traversal(tmp_path):
    workspace = Workspace(tmp_path)

    with pytest.raises(ValueError, match="escapes workspace"):
        workspace.resolve("../outside.txt")


def test_workspace_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("private")
    (tmp_path / "linked.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes workspace"):
        Workspace(tmp_path).resolve("linked.txt")


def test_command_policy_classifies_commands():
    policy = CommandPolicy()

    assert policy.classify("pytest -q") == CommandRisk.SAFE
    assert policy.classify("pip install httpx") == CommandRisk.ASK
    assert policy.classify("rm -rf /") == CommandRisk.BLOCK
    assert policy.classify("arbitrary-command") == CommandRisk.ASK


def test_shell_blocks_and_requires_approval(tmp_path):
    tool = RunCommandTool(str(tmp_path))

    blocked = asyncio.run(tool.execute("rm -rf /"))
    approval = asyncio.run(tool.execute("pip install package"))

    assert not blocked.success
    assert "blocked" in blocked.output
    assert not approval.success
    assert "approval" in approval.output


def test_shell_runs_safe_command_inside_workspace(tmp_path):
    tool = RunCommandTool(str(tmp_path))

    result = asyncio.run(tool.execute("pwd"))

    assert result.success
    assert str(tmp_path) in result.output
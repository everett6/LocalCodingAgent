"""Tests for the human-approval hook on ASK-risk tool actions.

Before this, an ASK-risk command (CommandPolicy.ASK) was an unconditional
dead end: RunCommandTool always returned success=False with no way for a
human to ever actually approve it, and GitCommitTool/GitCheckoutTool had no
gating at all (they'd run immediately regardless of risk). These tests
cover the callback-driven approve/deny paths and the auto-approve config
escape hatch.
"""
import asyncio

from local_coder.tools.git import GitCheckoutTool, GitCommitTool
from local_coder.tools.shell import RunCommandTool
from local_coder.types import ApprovalConfig


def run(coro):
    return asyncio.run(coro)


def test_ask_command_denied_with_no_callback_wired(tmp_path):
    tool = RunCommandTool(str(tmp_path))

    result = run(tool.execute("pip install requests"))

    assert not result.success
    assert "approval" in result.output


def test_ask_command_approved_via_callback(tmp_path):
    async def approve(_description: str) -> bool:
        return True

    tool = RunCommandTool(str(tmp_path), approval_callback=approve)

    result = run(tool.execute("pwd"))  # SAFE, sanity check callback isn't required for safe commands
    assert result.success

    ask_tool = RunCommandTool(str(tmp_path), approval_callback=approve)
    result = run(ask_tool.execute("pip --version"))  # ASK-risk (startswith "pip")

    assert result.success


def test_ask_command_denied_via_callback(tmp_path):
    seen = []

    async def deny(description: str) -> bool:
        seen.append(description)
        return False

    tool = RunCommandTool(str(tmp_path), approval_callback=deny)

    result = run(tool.execute("pip install requests"))

    assert not result.success
    assert "denied" in result.output
    assert seen and "pip install requests" in seen[0]


def test_ask_command_auto_approved_when_config_disables_prompting(tmp_path):
    tool = RunCommandTool(
        str(tmp_path),
        approval=ApprovalConfig(require_approval_for_commands=False),
    )

    result = run(tool.execute("pwd"))
    assert result.success

    result = run(tool.execute("pip --version"))
    assert result.success  # ASK-risk, but require_approval_for_commands=False skips the gate


def test_blocked_command_ignores_approval_settings(tmp_path):
    tool = RunCommandTool(
        str(tmp_path),
        approval=ApprovalConfig(require_approval_for_commands=False),
    )

    result = run(tool.execute("rm -rf /"))

    assert not result.success
    assert "blocked" in result.output


def test_git_commit_requires_approval_by_default(tmp_path):
    tool = GitCommitTool(str(tmp_path))

    result = run(tool.execute(message="test commit"))

    assert not result.success
    assert "approval" in result.output


def test_git_commit_denied_via_callback(tmp_path):
    async def deny(_description: str) -> bool:
        return False

    tool = GitCommitTool(str(tmp_path), approval_callback=deny)

    result = run(tool.execute(message="test commit"))

    assert not result.success
    assert "denied" in result.output


def test_git_checkout_requires_approval_by_default(tmp_path):
    tool = GitCheckoutTool(str(tmp_path))

    result = run(tool.execute(branch="some-branch"))

    assert not result.success
    assert "approval" in result.output

"""Tool registry initialization for local_coder."""

from local_coder.approval import ApprovalCallback
from local_coder.tools.base import ToolRegistry
from local_coder.tools.filesystem import ReadFileTool, WriteFileTool, ListFilesTool, ApplyPatchTool
from local_coder.tools.search import SearchFilesTool, GrepTool
from local_coder.tools.git import GitStatusTool, GitDiffTool, GitLogTool, GitCommitTool, GitCheckoutTool
from local_coder.tools.shell import RunCommandTool
from local_coder.tools.testing import RunTestsTool, BuildTool
from local_coder.types import ApprovalConfig


def create_tool_registry(
    project_root: str,
    approval: ApprovalConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
) -> ToolRegistry:
    """Create a ToolRegistry and register all available tools.

    approval/approval_callback are forwarded to every tool that can trigger
    an ASK-risk action (shell commands, git commits, git checkouts) so a
    single human-approval hook covers the whole registry.
    """
    registry = ToolRegistry()

    # Filesystem tools
    registry.register(ReadFileTool(project_root))
    registry.register(WriteFileTool(project_root))
    registry.register(ListFilesTool(project_root))
    registry.register(ApplyPatchTool(project_root))

    # Search tools
    registry.register(SearchFilesTool(project_root))
    registry.register(GrepTool(project_root))

    # Git tools
    registry.register(GitStatusTool(project_root))
    registry.register(GitDiffTool(project_root))
    registry.register(GitLogTool(project_root))
    registry.register(GitCommitTool(project_root, approval, approval_callback))
    registry.register(GitCheckoutTool(project_root, approval, approval_callback))

    # Shell tools
    registry.register(RunCommandTool(project_root, approval, approval_callback))

    # Testing tools
    registry.register(RunTestsTool(project_root))
    registry.register(BuildTool(project_root))

    return registry

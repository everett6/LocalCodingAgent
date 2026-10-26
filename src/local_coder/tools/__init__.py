"""Tool registry initialization for local_coder."""

from local_coder.approval import ApprovalCallback
from local_coder.tools.base import ToolRegistry
from local_coder.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListFilesTool, ApplyPatchTool
from local_coder.tools.search import SearchFilesTool, GrepTool
from local_coder.tools.git import GitStatusTool, GitDiffTool, GitLogTool, GitCommitTool, GitCheckoutTool
from local_coder.tools.shell import RunCommandTool
from local_coder.tools.testing import RunTestsTool, BuildTool
from local_coder.tools.security import (
    RunSASTTool, AuditDependenciesTool, AuditConfigTool, RecordFindingTool, SecurityScanTool,
)
from local_coder.tools.quality import LintTool, FormatCodeTool
from local_coder.tools.code_search import CodeSearchTool
from local_coder.tools.repo_map import RepoMapTool
from local_coder.types import ApprovalConfig
from typing import Any
from local_coder.tools.bugs import RunBugScanTool


def create_tool_registry(
    project_root: str,
    approval: ApprovalConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
    safety_harness: Any = None,
) -> ToolRegistry:
    """Create a ToolRegistry and register all available tools.

    approval/approval_callback are forwarded to every tool that can trigger
    an ASK-risk action (shell commands, git commits, git checkouts) so a
    single human-approval hook covers the whole registry.
    safety_harness controls OutputFilter syntax checks and Safety Tiers.
    """
    if safety_harness is None:
        from local_coder.safety import SafetyHarness
        safety_harness = SafetyHarness()

    registry = ToolRegistry()
    from local_coder.security_playbooks import SecurityPlaybookTool
    registry.register(SecurityPlaybookTool())
    from local_coder.tools.context_archive import RecoverContextTool
    registry.register(RecoverContextTool(registry))
    for allowed in registry._permissions.values():
        allowed.add(RecoverContextTool.name)

    # Filesystem tools
    registry.register(ReadFileTool(project_root))
    registry.register(WriteFileTool(project_root, safety_harness=safety_harness))
    registry.register(EditFileTool(project_root, safety_harness=safety_harness))
    registry.register(ListFilesTool(project_root))
    registry.register(ApplyPatchTool(project_root, safety_harness=safety_harness))

    # Search tools
    registry.register(SearchFilesTool(project_root))
    registry.register(GrepTool(project_root))
    code_search = CodeSearchTool(project_root)
    registry.register(code_search)
    registry.register(RepoMapTool(project_root, index=code_search.index))

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

    # Security & Audit tools
    registry.register(RunSASTTool(project_root))
    registry.register(AuditDependenciesTool(project_root))
    registry.register(AuditConfigTool(project_root))
    registry.register(RunBugScanTool(project_root))

    # Code quality and durable security review tools
    registry.register(LintTool(project_root))
    registry.register(FormatCodeTool(project_root))
    registry.register(SecurityScanTool(project_root))
    registry.register(RecordFindingTool(project_root))

    return registry

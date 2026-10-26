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
    registry.project_root = project_root
    registry.approval_callback = approval_callback
    registry.safety_harness = safety_harness
    from local_coder.memory.context_store import ContextMemory
    from local_coder.tools.memory import MemorySearchTool, MemoryWriteTool, TaskMemoryTool
    from local_coder.context.language_service import LanguageService
    from local_coder.tools.language_context import LanguageContextTool
    from local_coder.harness.evolution import EvolutionStore
    from local_coder.tools.evolution import HarnessEvolveTool, DelegateTaskTool, GeneratedToolTool
    from local_coder.core.sandbox import SandboxRunner
    from local_coder.core.snapshots import FileSnapshots
    from local_coder.integrations.mcp import MCPManager
    from local_coder.tools.mcp import MCPTool
    from local_coder.skills import SkillLibrary, SkillLibraryTool
    from local_coder.types import AgentRole, ToolName
    registry.context_memory = ContextMemory(project_root)
    from local_coder.statedir import state_path
    import json
    language_config = state_path(project_root, ".local-coder/language/settings.json")
    language_settings = json.loads(language_config.read_text()) if language_config.is_file() and language_config.stat().st_size <= 64000 else {}
    registry.language_service = LanguageService(project_root, server_commands=language_settings.get("server_commands"), auto_discover=language_settings.get("auto_discover", False))
    registry.evolution = EvolutionStore(project_root)
    registry.snapshots = FileSnapshots(project_root)
    registry.sandbox = SandboxRunner()
    registry.mcp = MCPManager(project_root, approval_callback=approval_callback)
    registry.memory_context_chars = 3000
    registry.memory_enabled = True
    registry.profile_overrides = {}
    registry.auto_language_context = False
    registry.pre_edit_snapshots = True
    from local_coder.tools.exploit import GenerateExploitTool, PrivilegeEscalationAnalysisTool, GenerateFuzzCasesTool
    for cls in (GenerateExploitTool, PrivilegeEscalationAnalysisTool, GenerateFuzzCasesTool):
        registry.register(cls(project_root, safety_harness=safety_harness, approval_callback=approval_callback))
    for tool in (MemorySearchTool(registry.context_memory), MemoryWriteTool(registry.context_memory), TaskMemoryTool(registry.context_memory),
                 LanguageContextTool(registry.language_service), MCPTool(registry.mcp), SkillLibraryTool(SkillLibrary(project_root)),
                 HarnessEvolveTool(registry.evolution, safety_harness, approval_callback, validator=registry.evaluate_revision),
                 DelegateTaskTool(), GeneratedToolTool(registry.evolution, runner=registry.sandbox.run)):
        registry.register(tool)
    for role, allowed in registry._permissions.items():
        if role != AgentRole.RED_TEAM:
            allowed.update({ToolName.MEMORY_SEARCH, ToolName.MEMORY_WRITE, ToolName.MEMORY_TASK_STATE, ToolName.LANGUAGE_CONTEXT, ToolName.SKILL_LIBRARY})
        if role in {AgentRole.CODER, AgentRole.DEBUGGER, AgentRole.ORCHESTRATOR}:
            allowed.update({ToolName.HARNESS_EVOLVE, ToolName.GENERATED_TOOL, ToolName.DELEGATE_TASK, ToolName.MCP_CALL})
        if role in {AgentRole.EXPLORER, AgentRole.PLANNER, AgentRole.REVIEWER}:
            allowed.add(ToolName.MCP_CALL)
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

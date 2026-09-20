"""Tool registry initialization for local_coder."""

from local_coder.tools.base import ToolRegistry
from local_coder.tools.filesystem import ReadFileTool, WriteFileTool, ListFilesTool, ApplyPatchTool
from local_coder.tools.search import SearchFilesTool, GrepTool
from local_coder.tools.git import GitStatusTool, GitDiffTool, GitLogTool, GitCommitTool, GitCheckoutTool
from local_coder.tools.shell import RunCommandTool
from local_coder.tools.testing import RunTestsTool, BuildTool


def create_tool_registry(project_root: str) -> ToolRegistry:
    """Create a ToolRegistry and register all available tools."""
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
    registry.register(GitCommitTool(project_root))
    registry.register(GitCheckoutTool(project_root))
    
    # Shell tools
    registry.register(RunCommandTool(project_root))
    
    # Testing tools
    registry.register(RunTestsTool(project_root))
    registry.register(BuildTool(project_root))
    
    return registry

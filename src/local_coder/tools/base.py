"""Base tool system with registry and permissions."""
from __future__ import annotations
import abc
import time
from typing import Any

from local_coder.types import ToolResult, ToolName, AgentRole
from local_coder.workspace import Workspace

# Tools that write files, keyed by the argument holding the target path.
FILE_WRITING_TOOLS = {ToolName.WRITE_FILE: "path", ToolName.EDIT_FILE: "path"}


class Tool(abc.ABC):
    """Base class for all tools."""
    
    name: ToolName
    description: str
    parameters: dict[str, Any]  # JSON Schema for parameters
    
    @abc.abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        ...
    
    def to_schema(self) -> dict:
        """Convert to OpenAI-compatible function schema for model tool calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name.value,
                "description": self.description,
                "parameters": self.parameters,
            }
        }


class ToolRegistry:
    """Registry of available tools with permission checking."""

    # Roles whose file writes are confined to one directory of the
    # workspace. Enforced here rather than trusted to the prompt.
    WRITE_SCOPES: dict[AgentRole, str] = {
        AgentRole.EXPLOIT_VALIDATOR: "tests/security_poc",
    }
    
    def __init__(self):
        self._tools: dict[ToolName, Tool] = {}
        self._permissions: dict[AgentRole, set[ToolName]] = {
            # Default permissions
            AgentRole.EXPLORER: {
                ToolName.READ_FILE, ToolName.LIST_FILES, 
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_LOG, ToolName.GIT_DIFF,
            },
            AgentRole.PLANNER: {
                ToolName.READ_FILE, ToolName.LIST_FILES,
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_LOG, ToolName.GIT_DIFF,
            },
            AgentRole.CODER: {
                ToolName.READ_FILE, ToolName.WRITE_FILE, ToolName.EDIT_FILE, ToolName.APPLY_PATCH,
                ToolName.LIST_FILES, ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG, ToolName.GIT_COMMIT,
                ToolName.RUN_COMMAND, ToolName.RUN_TESTS, ToolName.BUILD,
                ToolName.LINT, ToolName.FORMAT_CODE, ToolName.SECURITY_SCAN,
            },
            AgentRole.DEBUGGER: {
                ToolName.READ_FILE, ToolName.WRITE_FILE, ToolName.EDIT_FILE,
                ToolName.LIST_FILES, ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
                ToolName.RUN_COMMAND, ToolName.RUN_TESTS,
                ToolName.LINT, ToolName.FORMAT_CODE, ToolName.SECURITY_SCAN,
            },
            AgentRole.TESTER: {
                ToolName.READ_FILE, ToolName.WRITE_FILE, ToolName.EDIT_FILE,
                ToolName.LIST_FILES, ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF,
                ToolName.RUN_COMMAND, ToolName.RUN_TESTS, ToolName.BUILD,
                ToolName.LINT, ToolName.FORMAT_CODE,
            },
            AgentRole.REVIEWER: {
                ToolName.READ_FILE, ToolName.LIST_FILES,
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
                ToolName.RUN_TESTS, ToolName.LINT, ToolName.SECURITY_SCAN,
            },
            AgentRole.SECURITY: {
                ToolName.READ_FILE, ToolName.LIST_FILES,
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
                ToolName.LINT, ToolName.SECURITY_SCAN,
            },
            # Red-team companion: reproduce an already-identified finding as a
            # local PoC test. It can write test files and run tests, but has no
            # tool that reaches outside the workspace (no shell, no git mutation).
            AgentRole.EXPLOIT_VALIDATOR: {
                ToolName.READ_FILE, ToolName.WRITE_FILE, ToolName.LIST_FILES,
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF,
                ToolName.SECURITY_SCAN, ToolName.RUN_TESTS,
            },
            AgentRole.ORCHESTRATOR: set(ToolName),  # Full access
        }

        # code_search and repo_map are read-only, so every role that can
        # grep can use them.
        for allowed in self._permissions.values():
            if ToolName.GREP in allowed:
                allowed.add(ToolName.CODE_SEARCH)
                allowed.add(ToolName.REPO_MAP)
    
    def register(self, tool: Tool) -> None:
        """Register a tool instance."""
        self._tools[tool.name] = tool
    
    def get_tool(self, name: ToolName) -> Tool | None:
        """Get a registered tool by name."""
        return self._tools.get(name)
    
    def get_tools_for_role(self, role: AgentRole) -> list[Tool]:
        """Get all tools available for a given role."""
        allowed_names = self._permissions.get(role, set())
        return [tool for name, tool in self._tools.items() if name in allowed_names]
    
    def get_schemas_for_role(self, role: AgentRole) -> list[dict]:
        """Get OpenAI-compatible function schemas for tools available to a role."""
        return [tool.to_schema() for tool in self.get_tools_for_role(role)]
    
    def has_permission(self, role: AgentRole, tool_name: ToolName) -> bool:
        """Check if a role has permission to execute a tool."""
        return tool_name in self._permissions.get(role, set())

    def _outside_write_scope(self, role: AgentRole, name: ToolName, tool: Tool, arguments: dict[str, Any]) -> str | None:
        """Return an error if role may not write where these arguments point."""
        scope = self.WRITE_SCOPES.get(role)
        if scope is None:
            return None
        if name == ToolName.APPLY_PATCH:
            return f"Role {role.value} may not apply patches; write files under {scope}/ instead"
        key = FILE_WRITING_TOOLS.get(name)
        if key is None:
            return None
        path = arguments.get(key)
        root = getattr(tool, "project_root", None)
        if isinstance(path, str) and root:
            workspace = Workspace(root)
            try:
                if workspace.resolve(path).is_relative_to(workspace.root / scope):
                    return None
            except ValueError:
                pass
        return f"Role {role.value} may only write files under {scope}/"
    
    async def execute_tool(
        self, role: AgentRole, tool_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        """Execute a tool with permission checking."""
        start_time = time.time()
        
        try:
            name_enum = ToolName(tool_name)
        except ValueError:
            return ToolResult(
                success=False, 
                output=f"Unknown tool: {tool_name}",
                duration_ms=int((time.time() - start_time) * 1000)
            )
            
        if not self.has_permission(role, name_enum):
            return ToolResult(
                success=False, 
                output=f"Role {role.value} does not have permission to use tool: {tool_name}",
                duration_ms=int((time.time() - start_time) * 1000)
            )
            
        tool = self.get_tool(name_enum)
        if not tool:
            return ToolResult(
                success=False, 
                output=f"Tool {tool_name} is not registered",
                duration_ms=int((time.time() - start_time) * 1000)
            )
            
        scope_error = self._outside_write_scope(role, name_enum, tool, arguments)
        if scope_error:
            return ToolResult(
                success=False,
                output=scope_error,
                duration_ms=int((time.time() - start_time) * 1000)
            )

        try:
            result = await tool.execute(**arguments)
            if not hasattr(result, "duration_ms") or result.duration_ms is None:
                result.duration_ms = int((time.time() - start_time) * 1000)
            return result
        except Exception as e:
            return ToolResult(
                success=False, 
                output=f"Error executing tool {tool_name}: {str(e)}",
                duration_ms=int((time.time() - start_time) * 1000)
            )

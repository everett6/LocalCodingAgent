"""Base tool system with registry and permissions."""
from __future__ import annotations
import abc
import time
from typing import Any

from local_coder.types import ToolResult, ToolName, AgentRole


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
                ToolName.READ_FILE, ToolName.WRITE_FILE, ToolName.APPLY_PATCH,
                ToolName.LIST_FILES, ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG, ToolName.GIT_COMMIT,
                ToolName.RUN_COMMAND, ToolName.RUN_TESTS, ToolName.BUILD,
            },
            AgentRole.DEBUGGER: {
                ToolName.READ_FILE, ToolName.WRITE_FILE,
                ToolName.LIST_FILES, ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
                ToolName.RUN_COMMAND, ToolName.RUN_TESTS,
            },
            AgentRole.TESTER: {
                ToolName.READ_FILE, ToolName.WRITE_FILE,
                ToolName.LIST_FILES, ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF,
                ToolName.RUN_COMMAND, ToolName.RUN_TESTS, ToolName.BUILD,
            },
            AgentRole.REVIEWER: {
                ToolName.READ_FILE, ToolName.LIST_FILES,
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
                ToolName.RUN_TESTS,
            },
            AgentRole.ORCHESTRATOR: set(ToolName),  # Full access
        }
    
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

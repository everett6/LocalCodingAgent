"""Base tool system with registry and permissions."""
from __future__ import annotations
import abc
import asyncio
import contextvars
import time
from typing import Any

from local_coder.types import ToolResult, ToolName, AgentRole, ToolSettings
from local_coder.workspace import Workspace
from local_coder.orchestrator.blocked import AgentBlocked
from local_coder.providers.quota import ProviderQuotaExhausted

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
    leaf_tool_names = {"read_file", "list_files", "search_files", "grep", "code_search", "task_memory", "skill_library"}
    
    def __init__(self):
        self._tools: dict[ToolName, Tool] = {}
        self.task_context = contextvars.ContextVar("local_coder_tool_task", default=None)
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
                ToolName.SECURITY_PLAYBOOK,
                ToolName.RUN_SAST, ToolName.AUDIT_DEPENDENCIES, ToolName.AUDIT_CONFIG,
                ToolName.READ_FILE, ToolName.LIST_FILES,
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
                ToolName.RUN_TESTS, ToolName.LINT, ToolName.SECURITY_SCAN,
            },
            AgentRole.SECURITY: {
                ToolName.SECURITY_PLAYBOOK, ToolName.RUN_TESTS,
                ToolName.RUN_SAST, ToolName.AUDIT_DEPENDENCIES, ToolName.AUDIT_CONFIG,
                ToolName.READ_FILE, ToolName.LIST_FILES,
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
                ToolName.LINT, ToolName.SECURITY_SCAN, ToolName.RECORD_FINDING,
            },
            # Red-team companion: reproduce an already-identified finding as a
            # local PoC test. It can write test files and run tests, but has no
            # tool that reaches outside the workspace (no shell, no git mutation).
            AgentRole.EXPLOIT_VALIDATOR: {
                ToolName.READ_FILE, ToolName.WRITE_FILE, ToolName.LIST_FILES,
                ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF,
                ToolName.SECURITY_SCAN, ToolName.RUN_TESTS, ToolName.RECORD_FINDING,
            },
            AgentRole.BUG_HUNTER: {
                ToolName.SECURITY_PLAYBOOK, ToolName.RUN_BUG_SCAN,
                ToolName.READ_FILE, ToolName.WRITE_FILE, ToolName.EDIT_FILE, ToolName.APPLY_PATCH,
                ToolName.LIST_FILES, ToolName.SEARCH_FILES, ToolName.GREP,
                ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
                ToolName.RUN_COMMAND, ToolName.RUN_TESTS, ToolName.BUILD,
                ToolName.LINT, ToolName.FORMAT_CODE,
            },
            # Engagement tools are granted only by bind_engagement after validating scope.
            AgentRole.RED_TEAM: set(),
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
    
    def apply_settings(self, settings: ToolSettings) -> None:
        """Apply the config file's `tools:` section.

        Tool names this build doesn't know are skipped rather than rejected,
        so a config written for a newer tool set still loads.
        """
        known = {tool.value: tool for tool in ToolName}
        for role_name, tool_names in settings.roles.items():
            try:
                role = AgentRole(role_name)
            except ValueError:
                continue
            if role == AgentRole.RED_TEAM:
                # Only bind_engagement can establish these scoped capabilities.
                continue
            self._permissions[role] = {known[n] for n in tool_names if n in known}
        for name in settings.disabled:
            tool = known.get(name)
            if tool is None:
                continue
            self._tools.pop(tool, None)
            for allowed in self._permissions.values():
                allowed.discard(tool)
        if settings.command_timeout is not None:
            for tool in self._tools.values():
                if hasattr(tool, "default_timeout"):
                    tool.default_timeout = settings.command_timeout

    async def evaluate_revision(self, revision):
        """Measure local contract checks; never trust model-reported test success."""
        started = time.monotonic()
        kind, payload = revision["kind"], revision["payload"]
        if kind == "tool":
            result = await self.sandbox.run(payload["source"], {"probe": True}, timeout=5, name=payload["name"])
            return {"success": result["success"] and bool(result.get("output", "").strip()),
                    "measurements": {"duration_ms": result["duration_ms"], "exit_code": abs(result["exit_code"]), "output_bytes": len(result.get("output", "").encode())}}
        from local_coder.models.profiles import validate_overlay, resolve_profile
        checks = 0
        if kind == "loop_plan":
            validate_overlay(payload)
            profile = resolve_profile(type("Config", (), {"model_id": "test", "profile": "generic"})(), AgentRole.CODER, payload)
            schemas = self.get_schemas_for_role(AgentRole.CODER)
            original = {s["function"]["name"] for s in schemas}
            if not {s["function"]["name"] for s in profile.apply_tools(schemas)}.issubset(original):
                return {"success": False, "measurements": {"checks": 1}}
            checks = 2
        else:
            text = payload["text"]
            if len(text) > 6000:
                return {"success": False, "measurements": {"characters": len(text), "checks": 1}}
            checks = 1
        return {"success": True, "measurements": {"checks": checks, "duration_ms": int((time.monotonic()-started)*1000)}}

    async def close(self):
        for name in ("language_service", "mcp"):
            service = getattr(self, name, None)
            if service is not None:
                await service.close()

    def _write_targets(self, name, arguments):
        if name in FILE_WRITING_TOOLS:
            return [arguments.get(FILE_WRITING_TOOLS[name])]
        if name == ToolName.APPLY_PATCH:
            from local_coder.tools.filesystem import patch_targets
            return patch_targets(self.project_root, arguments.get("patch", ""), arguments.get("working_dir"))
        return []

    def _leaf_scope(self, name, arguments):
        current = self.task_context.get() or {}
        if not current.get("depth"):
            return
        if name.value not in self.leaf_tool_names:
            raise ValueError("Leaf tasks have only scoped read and task-memory capabilities")
        if name == ToolName.MEMORY_TASK_STATE:
            if arguments.get("task_id") != current.get("task_id"):
                raise ValueError("Leaf memory must belong to the current task")
            return
        if name == ToolName.SKILL_LIBRARY:
            return
        workspace = Workspace(self.project_root)
        roots = [workspace.resolve(path) for path in current.get("files", [])] or [workspace.root]
        target = workspace.resolve(arguments.get("path") or ".")
        if not any(target == root or target.is_relative_to(root) for root in roots):
            raise ValueError("Tool path exceeds the leaf task's inherited scope; supply an in-scope path")

    async def _language_before_edit(self, paths):
        """Expose current symbols to the model before allowing an unread edit."""
        if not getattr(self, "auto_language_context", False) or (self.task_context.get() or {}).get("depth"):
            return ""
        import hashlib
        current = self.task_context.get() or {}
        cache = getattr(self, "_language_reads", None)
        if cache is None:
            cache = self._language_reads = set()
        context = []
        for path in paths:
            target = Workspace(self.project_root).resolve(path)
            if not target.is_file() or target.stat().st_size > self.language_service.max_file_bytes:
                continue
            identity = (current.get("task_id"), str(target), hashlib.sha256(target.read_bytes()).hexdigest())
            if identity in cache:
                continue
            result = await self.language_service.context(str(target), char_budget=1800)
            cache.add(identity)
            if len(cache) > 1000:
                cache.intersection_update({identity})
            context.append(result["text"])
        return "\n".join(context)[:6000]

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
            
        # Validate model-supplied JSON before scope inspection, snapshots, hooks
        # or execution. Engine-only arguments are injected after this gateway.
        from jsonschema import Draft202012Validator
        if not isinstance(arguments, dict) or any(not isinstance(key, str) or key.startswith("_") for key in arguments):
            return ToolResult(success=False, output="Tool arguments must be an object without engine-only fields")
        schema = {**tool.parameters, "additionalProperties": False}
        errors = list(Draft202012Validator(schema).iter_errors(arguments))
        if errors:
            # Do not echo argument values (which can contain credentials).
            return ToolResult(success=False, output="Tool arguments failed schema validation: " +
                              ", ".join(sorted({str(error.validator) for error in errors})))
        scope_error = self._outside_write_scope(role, name_enum, tool, arguments)
        if scope_error:
            return ToolResult(
                success=False,
                output=scope_error,
                duration_ms=int((time.time() - start_time) * 1000)
            )

        try:
            self._leaf_scope(name_enum, arguments)
            if name_enum in {ToolName.MEMORY_SEARCH, ToolName.MEMORY_WRITE, ToolName.MEMORY_TASK_STATE} and not getattr(self, "memory_enabled", True):
                raise ValueError("Project context memory is disabled in harness settings")
            if name_enum == ToolName.MEMORY_TASK_STATE and role == AgentRole.RED_TEAM:
                current = self.task_context.get() or {}
                if not current.get("task_id") or arguments.get("task_id") != current["task_id"]:
                    raise ValueError("Red team task memory is confined to the current engagement task")
            snapshot = None
            targets = self._write_targets(name_enum, arguments)
            root = getattr(self, "project_root", None)
            if root:
                for target in targets:
                    if not isinstance(target, str):
                        raise ValueError("File mutation requires an explicit workspace path")
                    path = Workspace(root).resolve(target).relative_to(Workspace(root).root)
                    if path.parts and path.parts[0] in {".git", ".local-coder"}:
                        raise ValueError("Private engine state must be changed through its dedicated tools")
            if targets:
                language = await self._language_before_edit(targets)
                if language:
                    return ToolResult(success=False, output="Review the current language context before retrying the edit (no files changed):\n" + language)
            if targets and getattr(self, "pre_edit_snapshots", False):
                snapshot = await asyncio.to_thread(self.snapshots.capture, targets)
            if name_enum in {ToolName.RECOVER_CONTEXT, ToolName.DELEGATE_TASK, ToolName.HARNESS_EVOLVE, ToolName.GENERATED_TOOL}:
                arguments = {**arguments, "_caller_role": role}
            if name_enum == ToolName.DELEGATE_TASK:
                current = self.task_context.get() or {}
                arguments = {**arguments, "_delegation_depth": current.get("depth", 0)}
            result = await tool.execute(**arguments)
            if result.success and name_enum == ToolName.READ_FILE:
                language = await self._language_before_edit([arguments["path"]])
                if language:
                    result.output += "\n\nLanguage context (untrusted evidence):\n" + language
            if snapshot:
                await asyncio.to_thread(self.snapshots.complete, snapshot["id"])
                result.data["snapshot_id"] = snapshot["id"]
            memory = getattr(self, "context_memory", None)
            if memory and getattr(self, "memory_enabled", True) and name_enum not in {ToolName.MEMORY_SEARCH, ToolName.MEMORY_WRITE, ToolName.MEMORY_TASK_STATE}:
                current = self.task_context.get() or {}
                kind = "finding" if name_enum == ToolName.RECORD_FINDING and result.success else "tool" if result.success else "failure"
                source = arguments.get("path") if name_enum in {ToolName.READ_FILE, ToolName.WRITE_FILE, ToolName.EDIT_FILE, ToolName.LANGUAGE_CONTEXT} else None
                await asyncio.to_thread(memory.ingest, kind, result.output[:16000], source_path=source,
                                        task_id=current.get("task_id"), metadata={"tool": tool_name, "success": result.success})
            if not hasattr(result, "duration_ms") or result.duration_ms is None:
                result.duration_ms = int((time.time() - start_time) * 1000)
            return result
        except (AgentBlocked, ProviderQuotaExhausted):
            raise
        except Exception as e:
            return ToolResult(
                success=False, 
                output=f"Error executing tool {tool_name}: {str(e)}",
                duration_ms=int((time.time() - start_time) * 1000)
            )

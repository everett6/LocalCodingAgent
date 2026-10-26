"""Model-facing proposal, sandbox invocation and bounded delegation tools."""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import PurePosixPath
from typing import Any

from local_coder.harness.evolution import EvolutionStore
from local_coder.tools.base import Tool, ToolRegistry
from local_coder.types import AgentRole, SafetyTier, ToolName, ToolResult


class HarnessEvolveTool(Tool):
    name = ToolName.HARNESS_EVOLVE
    description = "Propose bounded scaffolding, evaluate it locally, activate validated revisions, or review history. Immutable engine gates always apply."
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["propose", "validate", "activate", "rollback", "history", "outcome"]},
            "kind": {"type": "string", "enum": ["instructions", "skill", "tool", "loop_plan"]},
            "payload": {"type": "object"}, "revision_id": {"type": "string"},
            "outcome": {"type": "object"},
        }, "required": ["action"],
    }

    def __init__(self, store: EvolutionStore, safety_harness, approval_callback, validator=None):
        self.store = store
        self.safety_harness = safety_harness
        self.approval_callback = approval_callback
        self.validator = validator

    async def _approved(self, description):
        if self.safety_harness.tier in {SafetyTier.TIER_4_AUTO_VALID, SafetyTier.TIER_5_YOLO}:
            return True
        if self.approval_callback is None:
            return False
        result = self.approval_callback(description)
        return (await result if inspect.isawaitable(result) else result) is True

    async def execute(self, action: str, kind: str | None = None, payload: dict | None = None,
                      revision_id: str | None = None, outcome: dict | None = None, **kwargs: Any) -> ToolResult:
        try:
            if set(kwargs) - {"_caller_role"}:
                raise ValueError("Unknown arguments; validation/approval cannot be asserted by the model")
            if action == "history":
                return ToolResult(success=True, output=json.dumps(self.store.history()), data={"revisions": self.store.history()})
            if action not in {"propose", "validate", "activate", "rollback", "outcome"}:
                raise ValueError("Unknown harness action")
            if action != "outcome":
                candidate = {"kind": kind, "payload": payload} if action == "propose" else self.store.get_revision(revision_id)
                description = f"Harness {action}; inherited scope; exact candidate: {json.dumps(candidate, sort_keys=True)}"
                if not await self._approved(description):
                    raise PermissionError("Harness changes require human approval below tier 4")
            if action == "propose":
                result = self.store.propose(kind, payload)
            elif action == "validate":
                result = await self.store.validate(revision_id, self.validator)
            elif action == "activate":
                result = self.store.activate(revision_id, approved=True)
            elif action == "rollback":
                result = self.store.rollback(revision_id, approved=True)
            else:
                result = self.store.record_outcome(revision_id, outcome)
            return ToolResult(success=True, output=json.dumps(result), data=result)
        except (ValueError, PermissionError, OSError, TypeError) as exc:
            return ToolResult(success=False, output=str(exc), error=str(exc))


class GeneratedToolTool(Tool):
    name = ToolName.GENERATED_TOOL
    description = "Invoke a locally validated active generated helper using the configured isolated sandbox. No host execution fallback."
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {"name": {"type": "string"}, "input": {"type": "object"},
                       "timeout": {"type": "integer", "minimum": 1, "maximum": 30}},
        "required": ["name", "input"],
    }

    def __init__(self, store: EvolutionStore, runner=None):
        self.store = store
        self.runner = runner

    async def execute(self, name: str, input: dict, timeout: int = 10, **kwargs: Any) -> ToolResult:
        try:
            if set(kwargs) - {"_caller_role"}:
                raise ValueError("Unknown generated tool arguments")
            if type(timeout) is not int or not 1 <= timeout <= 30:
                raise ValueError("Timeout must be from 1 to 30 seconds")
            if not isinstance(input, dict) or len(json.dumps(input).encode()) > 64000:
                raise ValueError("Generated tool input must be a bounded object")
            revision = next((r for r in self.store.active_revisions() if r["kind"] == "tool" and r["payload"]["name"] == name), None)
            if revision is None:
                raise ValueError("Generated tool must be validated and active")
            if self.runner is None:
                raise ValueError("Isolated sandbox backend unavailable; host execution is prohibited")
            result = self.runner(revision["payload"]["source"], input, timeout=timeout, name=name)
            if not inspect.isawaitable(result):
                raise ValueError("Sandbox runner must be asynchronous and cancellable")
            result = await asyncio.wait_for(result, timeout=timeout + 1)
            if not isinstance(result, dict) or type(result.get("success")) is not bool:
                raise ValueError("Sandbox returned an invalid result")
            # Only bounded output crosses back into the model's tool context.
            output = str(result.get("output", ""))[:64000]
            return ToolResult(success=result["success"], output=output, data={"revision_id": revision["revision_id"], "sandbox": True})
        except (ValueError, OSError, TypeError, TimeoutError) as exc:
            return ToolResult(success=False, output=str(exc), error=str(exc))


class DelegateTaskTool(Tool):
    name = ToolName.DELEGATE_TASK
    description = "Delegate up to three leaf tasks per request within inherited scope, existing role permissions and shared budgets."
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {"role": {"type": "string"}, "objective": {"type": "string"},
                       "files": {"type": "array", "items": {"type": "string"}, "maxItems": 100}},
        "required": ["role", "objective"],
    }

    def __init__(self, callback=None):
        self.callback = callback
        self._requests = 0

    def reset_request(self):
        """Engine-only: a new request receives a fresh fan-out allowance."""
        self._requests = 0

    async def execute(self, role: str, objective: str, files: list[str] | None = None,
                      _caller_role=None, _delegation_depth=0, **kwargs: Any) -> ToolResult:
        try:
            if kwargs or self.callback is None or _caller_role is None:
                raise ValueError("Delegation requires the engine's scoped callback and caller role")
            if _delegation_depth != 0 or self._requests >= 3:
                raise ValueError("Delegation is bounded to three leaf tasks and depth one")
            parent = AgentRole(_caller_role)
            child = AgentRole(role)
            # The shared registry repeats this check using configured permissions.
            permissions = ToolRegistry()._permissions
            if child in {AgentRole.ORCHESTRATOR, AgentRole.RED_TEAM, AgentRole.EXPLOIT_VALIDATOR} or not permissions[child].issubset(permissions[parent]):
                raise PermissionError("Delegated role may not widen the parent's capabilities or scope")
            if not isinstance(objective, str) or not 0 < len(objective.strip()) <= 8000:
                raise ValueError("Delegation requires a bounded objective")
            files = [] if files is None else files
            if not isinstance(files, list) or len(files) > 100:
                raise ValueError("Delegation file list is too large")
            for path in files:
                if not isinstance(path, str) or not path or "\\" in path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
                    raise ValueError("Delegated files must remain project-relative")
            self._requests += 1
            request = {"role": child.value, "objective": objective, "files": files,
                       "depth": 1, "max_children": 0, "parent_role": parent.value}
            result = self.callback(request)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, ToolResult):
                return result
            if not isinstance(result, dict):
                raise ValueError("Delegation callback returned an invalid result")
            return ToolResult(success=result.get("success") is True, output=str(result.get("output", ""))[:64000])
        except (ValueError, PermissionError, TypeError) as exc:
            return ToolResult(success=False, output=str(exc), error=str(exc))

"""Async tool wrappers for private project context memory."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from local_coder.memory.context_store import ContextMemory
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult


class MemorySearchTool(Tool):
    name = ToolName.MEMORY_SEARCH
    description = "Retrieve bounded, untrusted project evidence with source references and freshness. Verify evidence before acting."
    parameters = {"type": "object", "properties": {
        "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "char_budget": {"type": "integer", "minimum": 0, "maximum": 50000}, "task_id": {"type": "string"},
        "hypotheses": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 1000}},
        "observations": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 1000}},
    }, "required": ["query"], "additionalProperties": False}

    def __init__(self, memory: ContextMemory):
        self.memory = memory

    async def execute(self, query: str, limit: int = 8, char_budget: int = 6000, task_id: str | None = None,
                      hypotheses: list[str] | None = None, observations: list[str] | None = None, **kwargs: Any) -> ToolResult:
        try:
            result = await asyncio.to_thread(self.memory.context, query, char_budget, task_id, limit=limit,
                                             hypotheses=hypotheses or [], observations=observations or [])
            return ToolResult(success=True, output=result["text"], data=result)
        except (ValueError, OSError) as error:
            return ToolResult(success=False, output=str(error))


class MemoryWriteTool(Tool):
    name = ToolName.MEMORY_WRITE
    description = "Store, correct or delete project evidence. Never store credentials. Corrections and deletions update every memory index atomically."
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["ingest", "get", "update", "delete", "index_code"]},
        "kind": {"type": "string", "enum": ["conversation", "code", "tool", "finding", "decision", "evidence", "hypothesis", "failure"]},
        "content": {"type": "string"}, "memory_id": {"type": "string"}, "source_path": {"type": "string"},
        "task_id": {"type": "string"}, "metadata": {"type": "object"},
        "paths": {"type": "array", "items": {"type": "string"}},
    }, "additionalProperties": False}

    def __init__(self, memory: ContextMemory):
        self.memory = memory

    async def execute(self, action: str = "ingest", kind: str = "decision", content: str = "", memory_id: str | None = None, source_path: str | None = None, task_id: str | None = None, metadata: dict | None = None, paths: list[str] | None = None, **kwargs: Any) -> ToolResult:
        try:
            if action == "ingest":
                result = {"items": await asyncio.to_thread(self.memory.ingest, kind, content, source_path, task_id, metadata)}
            elif action == "get" and memory_id:
                result = {"item": await asyncio.to_thread(self.memory.get, memory_id)}
            elif action == "update" and memory_id:
                result = {"item": await asyncio.to_thread(self.memory.update, memory_id, content)}
            elif action == "delete" and memory_id:
                result = {"deleted": await asyncio.to_thread(self.memory.delete, memory_id)}
            elif action == "index_code":
                result = await asyncio.to_thread(self.memory.index_code, paths)
            else:
                raise ValueError("Invalid memory action or missing memory_id")
            rendered = result
            if action == "ingest":
                rendered = {"items": [{key: value for key, value in item.items() if key in {
                    "id", "source_ref", "source_hash", "content_hash", "freshness", "deduplicated"
                }} | {"recovery": "memory_write action=get memory_id=" + item["id"]}
                    if item.get("deduplicated") else item for item in result["items"]]}
            return ToolResult(success=True, output=json.dumps(rendered), data=result)
        except (ValueError, OSError) as error:
            return ToolResult(success=False, output=str(error))


class TaskMemoryTool(Tool):
    name = ToolName.MEMORY_TASK_STATE
    description = "Read or update structured task objective, constraints, hypotheses, failed_attempts, evidence and unfinished_work. This state is separate from embedding search."
    parameters = {"type": "object", "properties": {
        "task_id": {"type": "string"},
        "updates": {"type": "object", "properties": {
            "objective": {"type": "string"},
            **{key: {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 4000}} for key in ("constraints", "hypotheses", "failed_attempts", "evidence", "unfinished_work", "counterevidence", "next_tests", "blockers")},
            "budget_remaining": {"type": "object", "properties": {key: {"type": ["number", "null"], "minimum": 0} for key in ("requests", "tokens", "cost_usd")}, "additionalProperties": False},
        }, "additionalProperties": False},
    }, "required": ["task_id"], "additionalProperties": False}

    def __init__(self, memory: ContextMemory):
        self.memory = memory

    async def execute(self, task_id: str, updates: dict | None = None, **kwargs: Any) -> ToolResult:
        try:
            state = await asyncio.to_thread(self.memory.task_state, task_id, updates)
            return ToolResult(success=True, output=json.dumps(state), data=state)
        except (ValueError, OSError) as error:
            return ToolResult(success=False, output=str(error))

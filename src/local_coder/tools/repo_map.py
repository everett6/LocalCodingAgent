"""Ranked repository map backed by the local code index."""
import asyncio
import time
from typing import Any

from local_coder.context.code_index import CodeIndex
from local_coder.context.repo_map import DEFAULT_MAP_TOKENS, RepoMap
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace

MAX_MAP_TOKENS = 8192


class RepoMapTool(Tool):
    name = ToolName.REPO_MAP
    description = (
        "Show a ranked outline of the repository: the most important files and "
        "their key classes and functions, with line numbers. Pass what you're "
        "working on (a description, identifiers or file paths) to center the map "
        "on it and on the code it depends on. Use it to get oriented, then "
        "read_file the line ranges you need."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional task description, identifiers or paths to center the map on"},
            "files": {
                "type": "array", "items": {"type": "string"},
                "description": "Optional files the work is about; the map favors them and what they use",
            },
            "max_tokens": {"type": "integer", "description": f"Approximate size of the map in tokens (default {DEFAULT_MAP_TOKENS})"},
        },
        "required": [],
    }

    def __init__(self, project_root: str, index: CodeIndex | None = None):
        self.project_root = project_root
        self.repo_map = RepoMap(project_root, index=index)

    async def execute(
        self,
        query: str = "",
        files: list[str] | None = None,
        max_tokens: int = DEFAULT_MAP_TOKENS,
        **kwargs: Any,
    ) -> ToolResult:
        start_t = time.time()
        try:
            workspace = Workspace(self.project_root)
            focus = [workspace.relative_path(f) for f in (files or [])]
            budget = max(64, min(int(max_tokens), MAX_MAP_TOKENS))
            content = await asyncio.to_thread(self.repo_map.build, query or "", focus, budget)
            if not content:
                content = "No source files with definitions were found."
            return ToolResult(success=True, output=content, duration_ms=int((time.time() - start_t) * 1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time() - start_t) * 1000))

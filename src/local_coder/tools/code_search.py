"""Ranked code search backed by the local retrieval index."""
import asyncio
import time
from typing import Any

from local_coder.context.code_index import CodeIndex
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace


class CodeSearchTool(Tool):
    name = ToolName.CODE_SEARCH
    description = (
        "Find the code most relevant to a description, ranked. Use this before grep "
        "when you don't know the exact text: describe what you're looking for "
        "(e.g. 'where tool permissions are checked', 'retry on model timeout') or give "
        "identifiers. Returns file:line ranges with the function/class name and the "
        "best-matching lines, so you can read_file just the range you need."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words or identifiers describing the code to find"},
            "path": {"type": "string", "description": "Optional directory or file to limit the search to"},
            "max_results": {"type": "integer", "description": "Max results to return (default 8, max 30)"},
            "snippets": {"type": "boolean", "description": "Include matching lines for each result (default true)"},
        },
        "required": ["query"],
    }

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.index = CodeIndex(project_root)

    async def execute(
        self,
        query: str,
        path: str | None = None,
        max_results: int = 8,
        snippets: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        start_t = time.time()
        try:
            prefix = None
            if path:
                workspace = Workspace(self.project_root)
                prefix = workspace.relative_path(path)
            limit = max(1, min(int(max_results), 30))

            def _search() -> str:
                hits = self.index.search(query, max_results=limit, path_prefix=prefix)
                if not hits:
                    return "No matching code found. Try other words, or grep for exact text."
                if snippets:
                    self.index.add_snippets(hits, query)
                out = []
                for hit in hits:
                    label = f"{hit.kind} {hit.symbol}" if hit.symbol else hit.kind
                    out.append(f"{hit.location()}  [{label}]  score={hit.score}")
                    for n, line in hit.snippet:
                        out.append(f"  {n}: {line}")
                return "\n".join(out)

            content = await asyncio.to_thread(_search)
            return ToolResult(success=True, output=content, duration_ms=int((time.time() - start_t) * 1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time() - start_t) * 1000))

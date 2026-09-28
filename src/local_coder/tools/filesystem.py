"""Filesystem tools for the coding agent."""
import asyncio
from pathlib import Path
import subprocess
import time
from typing import Any

from local_coder.workspace import Workspace
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult


def _resolve_and_check_path(project_root: str, path: str) -> Path:
    return Workspace(project_root).resolve(path)


class ReadFileTool(Tool):
    name = ToolName.READ_FILE
    description = (
        "Read a file's contents. Large files are returned a page at a time; the output "
        "ends with the start_line to pass to read the next page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to project root"},
            "start_line": {"type": "integer", "description": "Optional 1-indexed start line"},
            "end_line": {"type": "integer", "description": "Optional 1-indexed end line"}
        },
        "required": ["path"]
    }
    # One page is bounded on lines and characters, and one pathological
    # line (minified code, an embedded blob) can't eat the whole page.
    max_chars = 10000
    max_lines = 1000
    max_line_chars = 1000

    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, path: str, start_line: int | None = None, end_line: int | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            target = _resolve_and_check_path(self.project_root, path)
            if not target.is_file():
                return ToolResult(success=False, output=f"File not found: {path}")
                
            def _read() -> str:
                with open(target, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                total = len(lines)
                first = max(1, start_line or 1)
                last = min(end_line or total, total)
                if total and first > total:
                    return f"[start_line {first} is past the end of {path} ({total} lines)]"
                page: list[str] = []
                used = 0
                for line in lines[first - 1:last]:
                    if len(line) > self.max_line_chars:
                        line = line[:self.max_line_chars] + f"...[line truncated, {len(line)} chars]\n"
                    if page and (len(page) >= self.max_lines or used + len(line) > self.max_chars):
                        break
                    page.append(line)
                    used += len(line)
                content = "".join(page)
                shown_last = first + len(page) - 1
                if shown_last < last:
                    if not content.endswith("\n"):
                        content += "\n"
                    content += (
                        f"[Showing lines {first}-{shown_last} of {total}. "
                        f"Call read_file with start_line={shown_last + 1} to continue.]"
                    )
                return content
                
            content = await asyncio.to_thread(_read)
            return ToolResult(success=True, output=content, duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))


class WriteFileTool(Tool):
    name = ToolName.WRITE_FILE
    description = "Write content to a file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to project root"},
            "content": {"type": "string", "description": "Content to write"}
        },
        "required": ["path", "content"]
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, path: str, content: str, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            target = _resolve_and_check_path(self.project_root, path)
            
            def _write() -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content)
                    
            await asyncio.to_thread(_write)
            return ToolResult(
                success=True,
                output=f"Wrote to {path}",
                files_changed=[path],
                duration_ms=int((time.time()-start_t)*1000),
            )
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))


class ListFilesTool(Tool):
    name = ToolName.LIST_FILES
    description = "List files in a directory."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path relative to root"},
            "recursive": {"type": "boolean", "description": "Recursive list"},
            "pattern": {"type": "string", "description": "Optional glob pattern"}
        },
        "required": ["path"]
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, path: str, recursive: bool = False, pattern: str | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            target = _resolve_and_check_path(self.project_root, path if path else ".")
            if not target.is_dir():
                return ToolResult(success=False, output=f"Directory not found: {path}")
                
            def _list() -> str:
                results = []
                exclude = {".git", "__pycache__", "node_modules", ".venv"}
                it = target.rglob(pattern or "*") if recursive else target.glob(pattern or "*")
                
                for p in it:
                    if any(part in exclude for part in p.relative_to(target).parts):
                        continue
                    rel = p.relative_to(Path(self.project_root))
                    results.append(f"{rel}/" if p.is_dir() else str(rel))
                return "\n".join(sorted(results))
                
            content = await asyncio.to_thread(_list)
            return ToolResult(success=True, output=content or "No files found", duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))


class ApplyPatchTool(Tool):
    name = ToolName.APPLY_PATCH
    description = "Apply a unified diff patch."
    parameters = {
        "type": "object",
        "properties": {
            "patch": {"type": "string", "description": "Unified diff patch content"},
            "working_dir": {"type": "string", "description": "Optional working dir"}
        },
        "required": ["patch"]
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, patch: str, working_dir: str | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            cwd = _resolve_and_check_path(self.project_root, working_dir or ".")
            proc = await asyncio.create_subprocess_exec(
                "patch", "-p1",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd
            )
            stdout, stderr = await proc.communicate(patch.encode())
            success = proc.returncode == 0
            res_str = stdout.decode()
            if stderr:
                res_str += f"\nStderr:\n{stderr.decode()}"
            return ToolResult(success=success, output=res_str.strip(), duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))

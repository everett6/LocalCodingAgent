"""Search tools for the coding agent."""
import asyncio
import shutil
from pathlib import Path
from typing import Any
import subprocess
import time

from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace


def _resolve_and_check_path(project_root: str, path: str) -> Path:
    return Workspace(project_root).resolve(path)


class SearchFilesTool(Tool):
    name = ToolName.SEARCH_FILES
    description = "Search for files by name pattern."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py')"},
            "path": {"type": "string", "description": "Optional search directory"}
        },
        "required": ["pattern"]
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, pattern: str, path: str | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            target = _resolve_and_check_path(self.project_root, path or ".")
            
            def _search() -> str:
                results = []
                exclude = {".git", "__pycache__", "node_modules", ".venv"}
                for p in target.rglob(pattern):
                    if any(part in exclude for part in p.relative_to(target).parts):
                        continue
                    results.append(str(p.relative_to(Path(self.project_root))))
                return "\n".join(sorted(results))
                
            content = await asyncio.to_thread(_search)
            return ToolResult(success=True, output=content or "No files found", duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))


class GrepTool(Tool):
    name = ToolName.GREP
    description = "Search file contents."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Optional search directory"},
            "file_pattern": {"type": "string", "description": "Optional file glob"},
            "max_results": {"type": "integer", "description": "Max results to return"}
        },
        "required": ["pattern"]
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, pattern: str, path: str | None = None, file_pattern: str | None = None, max_results: int = 50, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            target = _resolve_and_check_path(self.project_root, path or ".")
            
            has_rg = shutil.which("rg") is not None
            if has_rg:
                cmd = ["rg", "-n", "--no-heading", "--color", "never"]
                if file_pattern:
                    cmd.extend(["-g", file_pattern])
                cmd.extend(["--", pattern, str(target)])
            else:
                cmd = ["grep", "-rnE"]
                if file_pattern:
                    cmd.append(f"--include={file_pattern}")
                cmd.extend(["--", pattern, str(target)])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.project_root
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode not in (0, 1):
                return ToolResult(
                    success=False,
                    output="Search failed: " + (stderr.decode(errors="replace").strip()[:2000] or f"exit code {proc.returncode}"),
                    exit_code=proc.returncode,
                    duration_ms=int((time.time()-start_t)*1000),
                )
            
            lines = stdout.decode().strip().splitlines()
            if not lines:
                return ToolResult(success=True, output="No matches found.", duration_ms=int((time.time()-start_t)*1000))
                
            # One match in a minified or generated file can be a
            # multi-kilobyte line; the file:line prefix is what matters.
            out_lines = [
                line if len(line) <= 300 else line[:300] + f"...[{len(line)} chars]"
                for line in lines[:max_results]
            ]
            res_str = "\n".join(out_lines)
            if len(lines) > max_results:
                res_str += f"\n... (Showing {max_results} of {len(lines)} matches)"
                
            return ToolResult(success=True, output=res_str, duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))

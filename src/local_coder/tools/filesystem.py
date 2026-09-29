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


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _find_loose_match(content: str, old_string: str) -> int | None:
    """Return the 1-indexed line where old_string matches with whitespace ignored.

    Small local models often get indentation or trailing spaces slightly
    wrong; pointing at the near-miss lets them re-read and copy it exactly.
    """
    needle = [line.strip() for line in old_string.strip("\n").splitlines()]
    if not needle or not any(needle):
        return None
    haystack = [line.strip() for line in content.splitlines()]
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i:i + len(needle)] == needle:
            return i + 1
    return None


class EditFileTool(Tool):
    name = ToolName.EDIT_FILE
    description = (
        "Edit a file by replacing an exact snippet of its current text. "
        "old_string must match the file exactly (including indentation) and be unique "
        "unless replace_all is true. Read the file first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to project root"},
            "old_string": {"type": "string", "description": "Exact existing text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring one"}
        },
        "required": ["path", "old_string", "new_string"]
    }

    def __init__(self, project_root: str):
        self.project_root = project_root

    async def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        start_t = time.time()

        def _fail(message: str) -> ToolResult:
            return ToolResult(success=False, output=message, duration_ms=int((time.time()-start_t)*1000))

        try:
            target = _resolve_and_check_path(self.project_root, path)
            if not target.is_file():
                return _fail(f"File not found: {path}")
            if not old_string:
                return _fail("old_string must not be empty; use write_file to create a file")
            if old_string == new_string:
                return _fail("old_string and new_string are identical; nothing to change")

            def _edit() -> ToolResult:
                # newline="" keeps CRLF files byte-for-byte instead of normalizing them.
                with open(target, "r", encoding="utf-8", newline="") as f:
                    content = f.read()

                old, new = old_string, new_string
                if old not in content and "\r\n" in content and "\r\n" not in old:
                    old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")

                count = content.count(old)
                if count == 0:
                    near = _find_loose_match(content, old_string)
                    if near is not None:
                        return _fail(
                            f"old_string not found exactly in {path}, but a whitespace-insensitive "
                            f"match starts at line {near}. Re-read those lines and copy them exactly."
                        )
                    return _fail(f"old_string not found in {path}. Re-read the file and copy the text exactly.")
                if count > 1 and not replace_all:
                    lines = []
                    pos = content.find(old)
                    while pos != -1:
                        lines.append(str(_line_number(content, pos)))
                        pos = content.find(old, pos + 1)
                    return _fail(
                        f"old_string matches {count} times in {path} (lines {', '.join(lines)}). "
                        "Include more surrounding context to make it unique, or set replace_all."
                    )

                first_line = _line_number(content, content.find(old))
                updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(updated)
                replaced = f"{count} occurrences" if count > 1 else "1 occurrence"
                return ToolResult(
                    success=True,
                    output=f"Edited {path}: replaced {replaced} (first at line {first_line})",
                    files_changed=[path],
                    duration_ms=int((time.time()-start_t)*1000),
                )

            return await asyncio.to_thread(_edit)
        except Exception as e:
            return _fail(f"Error: {str(e)}")


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


def _patched_files(patch: str) -> list[str]:
    """Paths a -p1 unified diff touches, taken from its ---/+++ headers."""
    files: list[str] = []
    old_path: str | None = None
    for line in patch.splitlines():
        if line.startswith("--- "):
            old_path = line[4:].split("\t")[0].strip()
        elif line.startswith("+++ ") and old_path is not None:
            new_path = line[4:].split("\t")[0].strip()
            # A deleted file's new side is /dev/null; report its old path instead.
            header = old_path if new_path == "/dev/null" else new_path
            old_path = None
            parts = header.split("/", 1)
            if header == "/dev/null" or len(parts) < 2:
                continue
            if parts[1] not in files:
                files.append(parts[1])
    return files


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
            files_changed = []
            if success:
                workspace = Workspace(self.project_root)
                for rel in _patched_files(patch):
                    try:
                        files_changed.append(workspace.relative_path(cwd / rel))
                    except ValueError:
                        continue
            return ToolResult(
                success=success,
                output=res_str.strip(),
                files_changed=files_changed,
                duration_ms=int((time.time()-start_t)*1000),
            )
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))

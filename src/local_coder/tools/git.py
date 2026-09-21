"""Git tools for the coding agent."""
import asyncio
from typing import Any
import subprocess
import time

from local_coder.approval import ApprovalCallback
from local_coder.tools.base import Tool
from local_coder.types import ApprovalConfig, ToolName, ToolResult
from local_coder.workspace import Workspace


def _safe_relative_path(project_root: str, path: str) -> str:
    """Validate a Git path against the workspace boundary."""
    return Workspace(project_root).relative_path(path)


class GitStatusTool(Tool):
    name = ToolName.GIT_STATUS
    description = "Run git status."
    parameters = {"type": "object", "properties": {}}
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "-s",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.project_root
            )
            stdout, stderr = await proc.communicate()
            res = stdout.decode().strip() or "No changes (working tree clean)."
            return ToolResult(success=proc.returncode == 0, output=res, duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))


class GitDiffTool(Tool):
    name = ToolName.GIT_DIFF
    description = "Run git diff."
    parameters = {
        "type": "object",
        "properties": {
            "staged": {"type": "boolean", "description": "Diff staged files"},
            "path": {"type": "string", "description": "Optional path"}
        }
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, staged: bool = False, path: str | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            cmd = ["git", "diff"]
            if staged:
                cmd.append("--staged")
            if path:
                cmd.extend(["--", _safe_relative_path(self.project_root, path)])
                
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.project_root
            )
            stdout, stderr = await proc.communicate()
            res = stdout.decode().strip() or "No diff."
            return ToolResult(success=proc.returncode == 0, output=res, duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))


class GitLogTool(Tool):
    name = ToolName.GIT_LOG
    description = "Run git log."
    parameters = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "Number of commits"},
            "path": {"type": "string", "description": "Optional path"}
        }
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, count: int = 10, path: str | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            cmd = ["git", "log", f"-n{count}", "--oneline"]
            if path:
                cmd.extend(["--", _safe_relative_path(self.project_root, path)])
                
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.project_root
            )
            stdout, stderr = await proc.communicate()
            res = stdout.decode().strip() or "No commits."
            return ToolResult(success=proc.returncode == 0, output=res, duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))


class GitCommitTool(Tool):
    name = ToolName.GIT_COMMIT
    description = "Create a commit."
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Commit message"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Files to stage"}
        },
        "required": ["message"]
    }

    def __init__(
        self,
        project_root: str,
        approval: ApprovalConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
    ):
        self.project_root = project_root
        self.approval = approval or ApprovalConfig()
        self.approval_callback = approval_callback

    async def execute(self, message: str, files: list[str] | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            if self.approval.require_approval_for_commits:
                if self.approval_callback is None:
                    return ToolResult(success=False, output="Commit requires approval before execution.", duration_ms=int((time.time()-start_t)*1000))
                approved = await self.approval_callback(f"Create git commit: {message!r}")
                if not approved:
                    return ToolResult(success=False, output="Commit denied by user.", duration_ms=int((time.time()-start_t)*1000))
            if files:
                safe_files = [_safe_relative_path(self.project_root, path) for path in files]
                add_cmd = ["git", "add"] + safe_files
                proc_add = await asyncio.create_subprocess_exec(
                    *add_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.project_root
                )
                await proc_add.communicate()
                
            proc = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", message,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.project_root
            )
            stdout, stderr = await proc.communicate()
            res = stdout.decode().strip()
            if stderr:
                res += f"\n{stderr.decode().strip()}"
            return ToolResult(success=proc.returncode == 0, output=res, duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))


class GitCheckoutTool(Tool):
    name = ToolName.GIT_CHECKOUT
    description = "Checkout a branch."
    parameters = {
        "type": "object",
        "properties": {
            "branch": {"type": "string", "description": "Branch name"},
            "create": {"type": "boolean", "description": "Create if not exists"}
        },
        "required": ["branch"]
    }

    def __init__(
        self,
        project_root: str,
        approval: ApprovalConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
    ):
        self.project_root = project_root
        self.approval = approval or ApprovalConfig()
        self.approval_callback = approval_callback

    async def execute(self, branch: str, create: bool = False, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            if self.approval.require_approval_for_commits:
                if self.approval_callback is None:
                    return ToolResult(success=False, output="Checkout requires approval before execution.", duration_ms=int((time.time()-start_t)*1000))
                approved = await self.approval_callback(f"Checkout branch: {branch} (create={create})")
                if not approved:
                    return ToolResult(success=False, output="Checkout denied by user.", duration_ms=int((time.time()-start_t)*1000))
            cmd = ["git", "checkout"]
            if create:
                cmd.append("-b")
            cmd.append(branch)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.project_root
            )
            stdout, stderr = await proc.communicate()
            res = stdout.decode().strip() or stderr.decode().strip()
            return ToolResult(success=proc.returncode == 0, output=res, duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))

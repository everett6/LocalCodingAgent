"""Shell tools for the coding agent."""
import asyncio
from pathlib import Path
from typing import Any
import time

from local_coder.approval import ApprovalCallback
from local_coder.tools.base import Tool
from local_coder.safety import CommandPolicy, CommandRisk
from local_coder.types import ApprovalConfig, ToolName, ToolResult
from local_coder.workspace import Workspace


def _resolve_and_check_path(project_root: str, path: str) -> Path:
    return Workspace(project_root).resolve(path)


class RunCommandTool(Tool):
    name = ToolName.RUN_COMMAND
    description = "Execute a shell command."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to run"},
            "working_dir": {"type": "string", "description": "Optional working dir"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)"}
        },
        "required": ["command"]
    }
    
    def __init__(
        self,
        project_root: str,
        approval: ApprovalConfig | None = None,
        approval_callback: ApprovalCallback | None = None,
    ):
        self.project_root = project_root
        self.policy = CommandPolicy()
        self.approval = approval or ApprovalConfig()
        self.approval_callback = approval_callback

    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 60, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            risk = self.policy.classify(command)
            if risk == CommandRisk.BLOCK:
                return ToolResult(success=False, output="Command blocked by security policy.", duration_ms=int((time.time()-start_t)*1000))
            if risk == CommandRisk.ASK and self.approval.require_approval_for_commands:
                if self.approval_callback is None:
                    return ToolResult(success=False, output="Command requires approval before execution.", duration_ms=int((time.time()-start_t)*1000))
                approved = await self.approval_callback(f"Run shell command: {command}")
                if not approved:
                    return ToolResult(success=False, output="Command denied by user.", duration_ms=int((time.time()-start_t)*1000))

            cwd = _resolve_and_check_path(self.project_root, working_dir or ".")
            
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                success = proc.returncode == 0
                out_str = stdout.decode()
                err_str = stderr.decode()
                
                res = []
                if out_str:
                    res.append(out_str)
                if err_str:
                    res.append(f"Stderr:\n{err_str}")
                    
                output = "\n".join(res)
                if len(output) > 50000:
                    output = output[:50000] + "\n...[TRUNCATED: exceeded 50000 chars]"
                    
                return ToolResult(success=success, output=output.strip() or "Command completed with no output.", duration_ms=int((time.time()-start_t)*1000))
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(success=False, output=f"Command timed out after {timeout} seconds.", duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))

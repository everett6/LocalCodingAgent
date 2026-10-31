"""Shell tools for the coding agent."""
import asyncio
import os
from pathlib import Path
import shutil
from typing import Any
import time
from uuid import uuid4
from local_coder.processes import communicate, process_options

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
            "timeout": {"type": "integer", "description": "Timeout in seconds (optional)"}
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
        # Used when the model doesn't pass a timeout; set from the config's
        # tools.command_timeout by ToolRegistry.apply_settings.
        self.default_timeout = 60
        self.sandbox_enabled = False
        self.sandbox_runtime = "auto"
        self.sandbox_image = "python:3.12-slim"

    async def execute(self, command: str, working_dir: str | None = None, timeout: int | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        if timeout is None:
            timeout = self.default_timeout
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
            container_name = None
            if self.sandbox_enabled:
                runtime = self._resolve_sandbox_runtime()
                if runtime is None:
                    return ToolResult(
                        success=False,
                        output=f"Sandbox runtime '{self.sandbox_runtime}' is unavailable; install Docker or Podman or disable tools.command_sandbox.",
                        duration_ms=int((time.time()-start_t)*1000),
                    )
                # Pre-flight: verify the image exists locally (no implicit pull).
                image_check = await self._check_local_image(runtime)
                if image_check is not None:
                    return ToolResult(
                        success=False,
                        output=image_check,
                        duration_ms=int((time.time()-start_t)*1000),
                    )
                container_name = f"local-coder-{uuid4().hex}"
                argv = self._sandbox_argv(runtime, container_name, command, cwd)
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.project_root,
                    **process_options()
                )
            else:
                runtime = None
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    **process_options()
                )
            try:
                stdout, stderr = await communicate(proc, timeout=timeout)
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
                cleanup_error = await self._cleanup_sandbox(runtime, container_name)
                detail = f"Command timed out after {timeout} seconds."
                if cleanup_error:
                    detail += f" Sandbox cleanup failed: {cleanup_error}"
                return ToolResult(success=False, output=detail, duration_ms=int((time.time()-start_t)*1000))
            except asyncio.CancelledError:
                await self._cleanup_sandbox(runtime, container_name)
                raise
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))

    def _resolve_sandbox_runtime(self) -> str | None:
        candidates = ("docker", "podman") if self.sandbox_runtime == "auto" else (self.sandbox_runtime,)
        return next((path for name in candidates if (path := shutil.which(name))), None)

    def _sandbox_argv(self, runtime: str, container_name: str, command: str, cwd: Path) -> list[str]:
        workspace = Workspace(self.project_root)
        relative_cwd = cwd.relative_to(workspace.root).as_posix()
        container_cwd = "/workspace" if relative_cwd == "." else f"/workspace/{relative_cwd}"
        uid = getattr(os, "getuid", lambda: 1000)()
        gid = getattr(os, "getgid", lambda: 1000)()
        return [
            runtime, "run", "--rm", "--pull=never", "--init",
            "--name", container_name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256", "--memory=1g", "--cpus=2",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
            "--user", f"{uid}:{gid}",
            "--mount", f"type=bind,source={workspace.root},target=/workspace",
            "--workdir", container_cwd,
            self.sandbox_image, "sh", "-lc", command,
        ]

    async def _cleanup_sandbox(self, runtime: str | None, container_name: str | None) -> str | None:
        if not runtime or not container_name:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                runtime, "rm", "--force", container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options()
            )
            stdout, stderr = await communicate(proc, timeout=10)
            if proc.returncode != 0 and b"No such container" not in stderr:
                return (stderr or stdout).decode(errors="replace").strip() or f"runtime exited {proc.returncode}"
        except Exception as exc:
            return str(exc)
        return None

    async def _check_local_image(self, runtime: str) -> str | None:
        """Return an error message if the sandbox image is not available locally."""
        try:
            proc = await asyncio.create_subprocess_exec(
                runtime, "image", "inspect", self.sandbox_image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options()
            )
            stdout, stderr = await communicate(proc, timeout=10)
            if proc.returncode != 0:
                return (
                    f"Sandbox image '{self.sandbox_image}' not found locally. "
                    f"Pull it first (e.g. `{runtime} pull {self.sandbox_image}`) "
                    f"or set a different tools.sandbox_image."
                )
        except Exception as exc:
            return f"Could not inspect sandbox image: {exc}"
        return None

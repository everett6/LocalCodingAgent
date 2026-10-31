"""Shell tools for the coding agent."""
import asyncio
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any
import time
from uuid import uuid4
from local_coder.processes import communicate, process_options, terminate_tree

from local_coder.approval import ApprovalCallback
from local_coder.tools.base import Tool
from local_coder.safety import CommandPolicy, CommandRisk
from local_coder.types import ApprovalConfig, ToolName, ToolResult
from local_coder.workspace import Workspace

logger = logging.getLogger(__name__)


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
        process_started = False
        runtime = None
        container_name = None

        def result(success: bool, output: str, *, unknown: bool = False, exit_code=None) -> ToolResult:
            data = {
                "execution_outcome": "unknown" if unknown else "completed" if process_started else "not_started",
                "mutation_completion_unknown": unknown,
            }
            if exit_code is not None:
                data["exit_code"] = exit_code
            return ToolResult(success=success, output=output, data=data, exit_code=exit_code,
                              duration_ms=int((time.time() - start_t) * 1000))

        try:
            if not isinstance(command, str) or not command or len(command) > 65536:
                return result(False, "Command must be nonempty text of at most 65536 characters.")
            if type(timeout) is not int or not 1 <= timeout <= 3600:
                return result(False, "Command timeout must be an integer from 1 to 3600 seconds.")
            risk = self.policy.classify(command)
            if risk == CommandRisk.BLOCK:
                return result(False, "Command blocked by security policy.")
            if risk == CommandRisk.ASK and self.approval.require_approval_for_commands:
                if self.approval_callback is None:
                    return result(False, "Command requires approval before execution.")
                approved = await self.approval_callback(f"Run shell command: {command}")
                if approved is not True:
                    return result(False, "Command denied by user.")

            cwd = _resolve_and_check_path(self.project_root, working_dir or ".")
            if self.sandbox_enabled:
                runtime = self._resolve_sandbox_runtime()
                if runtime is None:
                    return result(False, f"Sandbox runtime '{self.sandbox_runtime}' is unavailable; no command was started.")
                container_name = f"local-coder-{uuid4().hex}"
                argv = self._sandbox_argv(runtime, container_name, command, cwd)
                proc = await self._spawn_owned(asyncio.create_subprocess_exec,
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.project_root,
                    **process_options()
                )
            else:
                proc = await self._spawn_owned(asyncio.create_subprocess_shell,
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    **process_options()
                )
            process_started = True
            try:
                stdout, stderr = await communicate(proc, timeout=timeout)
                success = proc.returncode == 0
                out_str = (stdout or b"").decode(errors="replace")
                err_str = (stderr or b"").decode(errors="replace")
                
                res = []
                if out_str:
                    res.append(out_str)
                if err_str:
                    res.append(f"Stderr:\n{err_str}")
                    
                output = "\n".join(res)
                if len(output) > 50000:
                    output = output[:50000] + "\n...[TRUNCATED: exceeded 50000 chars]"
                    
                return result(success, output.strip() or "Command completed with no output.", exit_code=proc.returncode)
            except asyncio.TimeoutError:
                cleanup_error = await self._cleanup_sandbox(runtime, container_name)
                detail = f"Command timed out after {timeout} seconds; inspect workspace changes before retrying."
                if cleanup_error:
                    detail += f" Sandbox cleanup failed: {cleanup_error}"
                return result(False, detail, unknown=True)
        except asyncio.CancelledError as exc:
            cleanup_error = await self._cleanup_sandbox(runtime, container_name)
            if cleanup_error:
                message = "Sandbox cleanup failed after cancellation: " + cleanup_error
                logger.warning(message)
                exc.add_note(message)
            raise
        except Exception:
            cleanup_error = await self._cleanup_sandbox(runtime, container_name) if process_started else None
            detail = ("Command execution failed; inspect workspace changes before retrying."
                      if process_started else "Command could not be started; check the workspace and sandbox settings.")
            if cleanup_error:
                detail += " Sandbox cleanup failed: " + cleanup_error
            return result(False, detail, unknown=process_started)

    async def _spawn_owned(self, factory, *args, **kwargs):
        # A cancellation during process creation must not orphan a child before
        # its handle has been assigned to the caller. Finish acquiring that
        # handle, reap it, then let the outer handler remove its container.
        pending = asyncio.create_task(factory(*args, **kwargs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                process = await pending
                await terminate_tree(process)
            except Exception:
                logger.warning("Cancelled command launch cleanup could not be confirmed.")
            raise

    def _resolve_sandbox_runtime(self) -> str | None:
        if self.sandbox_runtime not in {"auto", "docker", "podman"}:
            raise ValueError("Unsupported sandbox runtime")
        candidates = ("docker", "podman") if self.sandbox_runtime == "auto" else (self.sandbox_runtime,)
        return next((path for name in candidates if (path := shutil.which(name))), None)

    def _sandbox_argv(self, runtime: str, container_name: str, command: str, cwd: Path) -> list[str]:
        workspace = Workspace(self.project_root)
        relative_cwd = cwd.relative_to(workspace.root).as_posix()
        container_cwd = "/workspace" if relative_cwd == "." else f"/workspace/{relative_cwd}"
        uid = getattr(os, "getuid", lambda: 1000)()
        gid = getattr(os, "getgid", lambda: 1000)()
        if uid == 0:
            raise ValueError("Sandbox commands require a non-root user")
        if any(character in str(workspace.root) for character in (",", "\n", "\r")):
            raise ValueError("Workspace cannot be represented as one container bind mount")
        if not isinstance(self.sandbox_image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}", self.sandbox_image):
            raise ValueError("Sandbox image must be a bounded local image reference")
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
            "--entrypoint=sh", self.sandbox_image, "-lc", command,
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
            _, stderr = await communicate(proc, timeout=10)
            if proc.returncode != 0 and b"No such container" not in (stderr or b""):
                return "Container removal was not confirmed."
        except Exception:
            return "Container removal did not finish within its cleanup deadline."
        return None

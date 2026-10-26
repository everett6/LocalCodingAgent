"""Lint and format tools for the coding agent.

Both tools pick from a fixed set of well-known linters/formatters instead of
accepting an arbitrary command, so they never need the shell approval flow:
the model can choose *which* known tool to run and on which workspace paths,
but not what gets executed.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace

MAX_OUTPUT_CHARS = 20000
EXCLUDED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "target", "dist", "build", ".local-coder"}

# Project marker files, in the order auto-detection tries languages.
LANGUAGE_MARKERS: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
    "javascript": ("package.json",),
    "go": ("go.mod",),
    "rust": ("Cargo.toml",),
}


@dataclass(frozen=True)
class QualityCommand:
    """One known linter or formatter invocation."""

    language: str
    executable: str
    args: tuple[str, ...]
    default_paths: tuple[str, ...] = (".",)
    accepts_paths: bool = True


LINTERS: dict[str, QualityCommand] = {
    "ruff": QualityCommand("python", "ruff", ("check",)),
    "flake8": QualityCommand("python", "flake8", ()),
    "eslint": QualityCommand("javascript", "eslint", ()),
    "go vet": QualityCommand("go", "go", ("vet",), default_paths=("./...",)),
    "clippy": QualityCommand("rust", "cargo", ("clippy", "--quiet"), accepts_paths=False),
}

FORMATTERS: dict[str, QualityCommand] = {
    "ruff": QualityCommand("python", "ruff", ("format",)),
    "black": QualityCommand("python", "black", ("-q",)),
    "prettier": QualityCommand("javascript", "prettier", ("--write",)),
    "gofmt": QualityCommand("go", "gofmt", ("-w",)),
    "rustfmt": QualityCommand("rust", "cargo", ("fmt",), accepts_paths=False),
}


def detect_languages(project_root: str) -> list[str]:
    root = Path(project_root)
    return [
        language
        for language, markers in LANGUAGE_MARKERS.items()
        if any((root / marker).exists() for marker in markers)
    ]


def find_executable(project_root: str, name: str) -> str | None:
    """Prefer a project-local node_modules binary, then PATH."""
    local = Path(project_root) / "node_modules" / ".bin" / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


async def run_bounded(argv: list[str], cwd: str, timeout: int) -> tuple[int, bytes, bytes] | None:
    """Run argv and return (returncode, stdout, stderr), or None on timeout.

    The command gets its own process group so a timeout also kills any
    children it spawned (formatters and scanners often fork workers).
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # Windows has no process groups here
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return None
    return proc.returncode, stdout, stderr


def _snapshot(root: Path, targets: list[Path]) -> dict[str, tuple[int, int]]:
    """Map workspace-relative file paths to (mtime_ns, size)."""
    state: dict[str, tuple[int, int]] = {}
    for target in targets:
        files = [target] if target.is_file() else target.rglob("*")
        for path in files:
            rel = path.relative_to(root)
            if any(part in EXCLUDED_DIRS for part in rel.parts) or not path.is_file():
                continue
            stat = path.stat()
            state[str(rel)] = (stat.st_mtime_ns, stat.st_size)
    return state


class _QualityTool(Tool):
    commands: dict[str, QualityCommand]
    kind: str

    def __init__(self, project_root: str, timeout: int = 120):
        self.project_root = project_root
        self.timeout = timeout

    def _select(self, tool: str) -> tuple[str, QualityCommand, str] | str:
        """Return (name, command, executable path), or an error message."""
        if tool != "auto":
            command = self.commands.get(tool)
            if command is None:
                return f"Unknown {self.kind} '{tool}'. Choose one of: auto, {', '.join(self.commands)}"
            executable = find_executable(self.project_root, command.executable)
            if executable is None:
                return f"{tool} is not installed (no '{command.executable}' in node_modules/.bin or PATH)."
            return tool, command, executable

        languages = detect_languages(self.project_root)
        if not languages:
            return f"Could not detect the project language, so no {self.kind} was chosen. Pass tool explicitly."
        for language in languages:
            for name, command in self.commands.items():
                if command.language != language:
                    continue
                executable = find_executable(self.project_root, command.executable)
                if executable is not None:
                    return name, command, executable
        options = [name for name, c in self.commands.items() if c.language in languages]
        return f"No {self.kind} is installed for this project ({', '.join(languages)}). Install one of: {', '.join(options)}"

    def _resolve_paths(self, command: QualityCommand, paths: list[str] | None) -> list[str]:
        if not command.accepts_paths:
            return []
        if not paths:
            return list(command.default_paths)
        workspace = Workspace(self.project_root)
        resolved = []
        for path in paths:
            rel = workspace.relative_path(path)
            # "./" keeps a path that starts with "-" from being read as an option.
            resolved.append(rel if not rel.startswith("-") else f"./{rel}")
        return resolved

    async def _run(self, tool: str, paths: list[str] | None) -> ToolResult:
        """Run the selected linter or formatter and capture its output."""
        start_t = time.time()

        def _result(success: bool, output: str) -> ToolResult:
            return ToolResult(success=success, output=output, duration_ms=int((time.time() - start_t) * 1000))

        selection = self._select(tool)
        if isinstance(selection, str):
            return _result(False, selection)
        name, command, executable = selection
        try:
            targets = self._resolve_paths(command, paths)
        except ValueError as e:
            return _result(False, f"Error: {e}")

        argv = [executable, *command.args, *targets]
        shown = " ".join([command.executable, *command.args, *targets])
        completed = await run_bounded(argv, self.project_root, self.timeout)
        if completed is None:
            return _result(False, f"$ {shown}\n{name} timed out after {self.timeout} seconds.")
        returncode, stdout, stderr = completed

        output = stdout.decode(errors="replace")
        if stderr:
            output += f"\nStderr:\n{stderr.decode(errors='replace')}"
        output = output.strip() or "No output."
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n...[TRUNCATED: exceeded {MAX_OUTPUT_CHARS} chars]"
        return _result(returncode == 0, f"$ {shown}\n{output}")


class LintTool(_QualityTool):
    name = ToolName.LINT
    kind = "linter"
    commands = LINTERS
    description = (
        "Run the project's linter and report problems without changing files. "
        "Auto-detects ruff/flake8, eslint, go vet, or cargo clippy."
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional paths relative to project root"},
            "tool": {"type": "string", "enum": ["auto", *LINTERS], "description": "Linter to run (default auto)"},
        },
    }

    async def execute(self, paths: list[str] | None = None, tool: str = "auto", **kwargs: Any) -> ToolResult:
        try:
            return await self._run(tool, paths)
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}")


class FormatCodeTool(_QualityTool):
    name = ToolName.FORMAT_CODE
    kind = "formatter"
    commands = FORMATTERS
    description = (
        "Format code in place with the project's formatter and report which files changed. "
        "Auto-detects ruff format/black, prettier, gofmt, or cargo fmt."
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional paths relative to project root"},
            "tool": {"type": "string", "enum": ["auto", *FORMATTERS], "description": "Formatter to run (default auto)"},
        },
    }

    async def execute(self, paths: list[str] | None = None, tool: str = "auto", **kwargs: Any) -> ToolResult:
        try:
            workspace = Workspace(self.project_root)
            # Snapshot the requested paths (or the whole workspace) so the
            # result can name the files the formatter actually rewrote.
            watched = [workspace.resolve(p) for p in (paths or ["."])]
            before = await asyncio.to_thread(_snapshot, workspace.root, watched)
            result = await self._run(tool, paths)
            after = await asyncio.to_thread(_snapshot, workspace.root, watched)
            changed = sorted(path for path, state in after.items() if before.get(path) != state)
            if result.success:
                result.files_changed = changed
                summary = f"Formatted {len(changed)} file(s): {', '.join(changed)}" if changed else "No files changed."
                result.output = f"{result.output}\n{summary}"
            return result
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}")

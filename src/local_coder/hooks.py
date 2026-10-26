"""Lifecycle hooks: user shell commands run around the agent's tool calls.

Hooks live in a ``hooks:`` section of the project config file, the same
file ``load_config`` reads (``--config``, ``.local-coder/config.yaml`` or
``config/config.yaml``)::

    hooks:
      after_edit:
        - command: ruff format "$LOCAL_CODER_FILE"
          timeout: 30
      after_tests: notify-send "local-coder: tests finished"
      before_tool:
        - tools: [run_command]
          command: ./scripts/audit-command.sh

Events:

    before_tool / after_tool   any tool call (narrow with ``tools:``)
    before_edit / after_edit   write_file, edit_file and apply_patch
    after_tests                run_tests

A ``before_*`` hook that exits non-zero blocks the call, and its output is
returned to the model as the tool's error, so a hook can veto a command or
an edit and say why. ``after_*`` hooks never change the result's success,
but their output is appended to it when they fail, so the model sees e.g. a
formatter's complaint. Each hook gets the details as environment variables
(``LOCAL_CODER_EVENT``, ``LOCAL_CODER_TOOL``, ``LOCAL_CODER_FILE``,
``LOCAL_CODER_SUCCESS``, ``LOCAL_CODER_PROJECT_ROOT``) and as one JSON
object on stdin, and runs with the project root as its working directory.

Hooks are arbitrary shell commands and a config file can arrive with a
cloned repository, so nothing runs until the user trusts the exact hooks
section (``local-coder hooks trust``). Trust is keyed by project path and a
hash of the section, and stored outside the project, so editing the hooks
(or cloning someone else's) needs a fresh approval.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from local_coder.types import ToolResult

EVENTS = ("before_tool", "after_tool", "before_edit", "after_edit", "after_tests")
EDIT_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
TEST_TOOLS = frozenset({"run_tests"})
DEFAULT_TIMEOUT = 60.0
MAX_OUTPUT_CHARS = 4000
MAX_STDIN_CHARS = 20000


@dataclass(frozen=True)
class Hook:
    event: str
    command: str
    tools: frozenset[str] = frozenset()  # empty = every tool the event covers
    timeout: float = DEFAULT_TIMEOUT

    def matches(self, tool_name: str) -> bool:
        return not self.tools or tool_name in self.tools


@dataclass
class HookConfig:
    hooks: list[Hook] = field(default_factory=list)
    source: Optional[Path] = None
    fingerprint: str = ""
    errors: list[str] = field(default_factory=list)

    def for_event(self, event: str, tool_name: str) -> list[Hook]:
        return [h for h in self.hooks if h.event == event and h.matches(tool_name)]


@dataclass
class HookOutcome:
    hook: Hook
    returncode: Optional[int]  # None when it timed out or could not start
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# === Loading ================================================================

def config_search_paths(config_path: Optional[str], project_root: str | os.PathLike) -> list[Path]:
    """Same order as orchestrator.config_loader.load_config."""
    paths = [Path(config_path)] if config_path else []
    root = Path(project_root)
    return paths + [root / ".local-coder" / "config.yaml", root / "config" / "config.yaml"]


def parse_hooks(data: Any) -> tuple[list[Hook], list[str]]:
    """Validate a ``hooks:`` mapping. Bad entries are skipped and reported."""
    hooks: list[Hook] = []
    errors: list[str] = []
    if data is None:
        return hooks, errors
    if not isinstance(data, dict):
        return hooks, ["hooks: must be a mapping of event name to commands"]
    for event, entries in data.items():
        if event not in EVENTS:
            errors.append(f"hooks.{event}: unknown event (expected one of {', '.join(EVENTS)})")
            continue
        if isinstance(entries, (str, dict)):
            entries = [entries]
        if not isinstance(entries, list):
            errors.append(f"hooks.{event}: expected a command or a list of commands")
            continue
        for i, entry in enumerate(entries):
            where = f"hooks.{event}[{i}]"
            if isinstance(entry, str):
                entry = {"command": entry}
            if not isinstance(entry, dict) or not isinstance(entry.get("command"), str) or not entry["command"].strip():
                errors.append(f"{where}: needs a non-empty 'command'")
                continue
            tools = entry.get("tools") or []
            if isinstance(tools, str):
                tools = [tools]
            if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                errors.append(f"{where}: 'tools' must be a tool name or a list of them")
                continue
            try:
                timeout = float(entry.get("timeout", DEFAULT_TIMEOUT))
            except (TypeError, ValueError):
                errors.append(f"{where}: 'timeout' must be a number of seconds")
                continue
            hooks.append(Hook(event=event, command=entry["command"].strip(),
                              tools=frozenset(tools), timeout=max(1.0, timeout)))
    return hooks, errors


def fingerprint(hooks: list[Hook]) -> str:
    canonical = json.dumps(
        [[h.event, h.command, sorted(h.tools), h.timeout] for h in hooks], sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_hooks(config_path: Optional[str], project_root: str | os.PathLike) -> HookConfig:
    for path in config_search_paths(config_path, project_root):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue  # load_config also falls through to the next file
        if not isinstance(data, dict):
            data = {}
        hooks, errors = parse_hooks(data.get("hooks"))
        return HookConfig(hooks=hooks, source=path, fingerprint=fingerprint(hooks) if hooks else "", errors=errors)
    return HookConfig()


# === Trust ==================================================================

def trust_file() -> Path:
    home = os.environ.get("LOCAL_CODER_HOME")
    base = Path(home) if home else Path.home() / ".config" / "local-coder"
    return base / "trusted-hooks.json"


def _read_trust() -> dict[str, str]:
    try:
        data = json.loads(trust_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _project_key(project_root: str | os.PathLike) -> str:
    return str(Path(project_root).resolve())


def is_trusted(config: HookConfig, project_root: str | os.PathLike) -> bool:
    if not config.hooks:
        return True
    return _read_trust().get(_project_key(project_root)) == config.fingerprint


def trust(config: HookConfig, project_root: str | os.PathLike) -> None:
    data = _read_trust()
    data[_project_key(project_root)] = config.fingerprint
    path = trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def untrust(project_root: str | os.PathLike) -> bool:
    data = _read_trust()
    if data.pop(_project_key(project_root), None) is None:
        return False
    trust_file().write_text(json.dumps(data, indent=2, sort_keys=True))
    return True


# === Running ================================================================

def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more chars]"


def _target_file(arguments: dict[str, Any]) -> str:
    for key in ("path", "file_path", "test_path"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return ""


class HookRunner:
    """Runs the configured hooks for tool-call events."""

    def __init__(self, config: HookConfig, project_root: str | os.PathLike):
        self.config = config
        self.project_root = str(project_root)

    def events_for(self, phase: str, tool_name: str) -> list[str]:
        events = [f"{phase}_tool"]
        if tool_name in EDIT_TOOLS:
            events.append(f"{phase}_edit")
        if phase == "after" and tool_name in TEST_TOOLS:
            events.append("after_tests")
        return events

    async def run(self, phase: str, tool_name: str, arguments: dict[str, Any],
                  result: Optional[ToolResult] = None) -> list[HookOutcome]:
        """Run every hook for this phase and tool, in config order.

        ``before`` stops at the first failing hook (the call is blocked
        anyway); ``after`` runs them all.
        """
        outcomes: list[HookOutcome] = []
        for event in self.events_for(phase, tool_name):
            for hook in self.config.for_event(event, tool_name):
                outcome = await self._run_one(hook, event, tool_name, arguments, result)
                outcomes.append(outcome)
                if phase == "before" and not outcome.ok:
                    return outcomes
        return outcomes

    async def _run_one(self, hook: Hook, event: str, tool_name: str,
                       arguments: dict[str, Any], result: Optional[ToolResult]) -> HookOutcome:
        env = dict(os.environ)
        env.update({
            "LOCAL_CODER_EVENT": event,
            "LOCAL_CODER_TOOL": tool_name,
            "LOCAL_CODER_FILE": _target_file(arguments),
            "LOCAL_CODER_PROJECT_ROOT": self.project_root,
        })
        payload: dict[str, Any] = {"event": event, "tool": tool_name, "arguments": arguments}
        if result is not None:
            env["LOCAL_CODER_SUCCESS"] = "1" if result.success else "0"
            payload["success"] = result.success
            payload["output"] = _truncate(result.output or "", MAX_STDIN_CHARS)
        stdin = json.dumps(payload, default=str).encode()

        try:
            proc = await asyncio.create_subprocess_shell(
                hook.command, cwd=self.project_root, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
        except OSError as e:
            return HookOutcome(hook, None, f"could not start hook: {e}")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=hook.timeout)
        except asyncio.TimeoutError:
            _kill(proc)
            await proc.wait()
            return HookOutcome(hook, None, f"hook timed out after {hook.timeout:g}s")
        except BaseException:
            _kill(proc)  # Ctrl+C / cancellation: don't leave the hook running
            raise
        return HookOutcome(hook, proc.returncode, _truncate(out.decode(errors="replace").strip(), MAX_OUTPUT_CHARS))


def _kill(proc) -> None:
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, OSError):  # no killpg on Windows; already gone
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _describe(outcome: HookOutcome) -> str:
    status = "timed out or failed to start" if outcome.returncode is None else f"exited {outcome.returncode}"
    text = f"[{outcome.hook.event} hook `{outcome.hook.command}` {status}]"
    return f"{text}\n{outcome.output}" if outcome.output else text


def install(registry, runner: HookRunner) -> None:
    """Wrap ``registry.execute_tool`` so every tool call runs the hooks.

    Done by wrapping rather than editing ToolRegistry so the registry stays
    hook-agnostic. Hooks run only for calls that would actually execute: an
    unknown tool, a missing permission or an unregistered tool is rejected
    by the original method without running any hook.
    """
    if not runner.config.hooks or getattr(registry, "_hooks_installed", False):
        return
    original = registry.execute_tool

    async def execute_tool(role, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        from local_coder.types import ToolName
        try:
            name_enum = ToolName(tool_name)
        except ValueError:
            return await original(role, tool_name, arguments)
        if not registry.has_permission(role, name_enum) or registry.get_tool(name_enum) is None:
            return await original(role, tool_name, arguments)

        before = await runner.run("before", tool_name, arguments)
        failed = next((o for o in before if not o.ok), None)
        if failed is not None:
            return ToolResult(success=False, output=f"Blocked by a hook.\n{_describe(failed)}", duration_ms=0)

        result = await original(role, tool_name, arguments)

        after = await runner.run("after", tool_name, arguments, result)
        notes = [_describe(o) for o in after if not o.ok]
        if notes:
            result.output = "\n\n".join([result.output or ""] + notes).strip()
        return result

    registry.execute_tool = execute_tool
    registry._hooks_installed = True

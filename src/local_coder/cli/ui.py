"""Terminal UI helpers for the interactive CLI.

Everything here is presentation only: the slash-command registry (which
feeds both /help and tab completion), closest-match suggestions, prompt
input with readline history, the startup banner, and the live progress
renderer that shows a spinner while a request runs.
"""
from __future__ import annotations

import contextlib
import difflib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

try:  # readline is missing on some platforms (e.g. stock Windows Python).
    import readline as _readline
except ImportError:  # pragma: no cover - platform dependent
    _readline = None


# === Slash commands =========================================================

@dataclass(frozen=True)
class SlashCommand:
    name: str
    help: str
    args: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    custom: bool = False  # loaded from a command file, not built in


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/plan", "Create a plan without executing", args="<request>"),
    SlashCommand("/review", "Review current uncommitted changes"),
    SlashCommand("/security", "Red/blue team security review (read-only)", args="[focus]"),
    SlashCommand("/validate-finding", "Reproduce an identified finding as a local PoC test", args="<finding>"),
    SlashCommand("/test", "Run tests and report results"),
    SlashCommand("/status", "Show system status"),
    SlashCommand("/checkpoint", "Save the current working tree"),
    SlashCommand("/checkpoints", "List saved checkpoints"),
    SlashCommand("/rollback", "Restore a checkpoint", args="<id>"),
    SlashCommand("/hooks", "Show configured hooks and whether they are trusted"),
    SlashCommand("/sessions", "List saved sessions"),
    SlashCommand("/resume", "Pick up a session's unfinished request", args="[id]"),
    SlashCommand("/new", "Start a new session for the next request"),
    SlashCommand("/help", "Show this help message"),
    SlashCommand("/quit", "Exit interactive mode", aliases=("/exit", "/q")),
)


# Custom commands from .local-coder/commands/ (see local_coder.custom_commands),
# set once the REPL has loaded them. Kept separate from COMMANDS so built-ins
# stay a constant and always come first in /help.
_custom_commands: tuple[SlashCommand, ...] = ()


def set_custom_commands(commands) -> None:
    """Register loaded CustomCommand objects for /help and completion."""
    global _custom_commands
    _custom_commands = tuple(
        SlashCommand(c.name, c.description, args=c.argument_hint, custom=True) for c in commands
    )


def all_commands() -> tuple[SlashCommand, ...]:
    return COMMANDS + _custom_commands


def builtin_names() -> frozenset[str]:
    return frozenset(command_names(COMMANDS))


def command_names(commands: tuple[SlashCommand, ...] | None = None) -> list[str]:
    """Every accepted slash command, primary names first, then aliases."""
    commands = all_commands() if commands is None else commands
    names = [c.name for c in commands]
    names += [alias for c in commands for alias in c.aliases]
    return names


def complete_command(text: str, commands: tuple[SlashCommand, ...] | None = None) -> list[str]:
    """Slash commands (including aliases) starting with ``text``, sorted."""
    if not text.startswith("/"):
        return []
    text = text.lower()
    return sorted(n for n in command_names(commands) if n.startswith(text))


def suggest_command(cmd: str, commands: tuple[SlashCommand, ...] | None = None) -> Optional[str]:
    """Closest known slash command to a mistyped one, or None."""
    cmd = cmd.lower()
    names = command_names(commands)
    prefixed = [n for n in names if n.startswith(cmd) and len(cmd) > 1]
    if len(prefixed) == 1:
        return prefixed[0]
    matches = difflib.get_close_matches(cmd, names, n=1, cutoff=0.6)
    return matches[0] if matches else None


def unknown_command_message(cmd: str) -> str:
    suggestion = suggest_command(cmd)
    message = f"[red]Unknown command:[/red] {cmd}"
    if suggestion:
        message += f"  Did you mean [bold]{suggestion}[/bold]?"
    return message + "  [dim](type /help for the list)[/dim]"


def build_help_table(commands: tuple[SlashCommand, ...] | None = None) -> Table:
    commands = all_commands() if commands is None else commands
    table = Table(title="Commands", title_justify="left", box=None, padding=(0, 2), show_header=False)
    table.add_column("Command", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    custom_heading_done = False
    for c in commands:
        if c.custom and not custom_heading_done:
            table.add_row("", "")
            table.add_row("[bold]Custom[/bold]", "[dim].local-coder/commands/[/dim]")
            custom_heading_done = True
        usage = f"{c.name} {c.args}".rstrip()
        if c.aliases:
            usage += ", " + ", ".join(c.aliases)
        # escape: "[focus]" would otherwise be parsed as rich markup and vanish.
        table.add_row(escape(usage), c.help)
    table.add_row("[dim]<anything else>[/dim]", "[dim]Run it as a coding request[/dim]")
    return table


# === Prompt input: history, completion, Ctrl+C handling =====================

HISTORY_LENGTH = 1000


def history_path(project_root: str | os.PathLike) -> Path:
    return Path(project_root) / ".local-coder" / "history"


def _ansi_prompt(console: Console, markup: str) -> str:
    """Render a rich-markup prompt to an ANSI string whose escape codes are
    wrapped in \\001/\\002 so readline measures the visible width correctly.

    rich's ``Console.input`` prints the prompt itself and then calls
    ``input()`` with an empty prompt, so readline thinks the prompt is zero
    columns wide and line editing / history recall erase or garble it.
    Passing the prompt to ``input()`` directly avoids that.
    """
    with console.capture() as capture:
        console.print(markup, end="")
    rendered = capture.get()
    out, i = [], 0
    while i < len(rendered):
        if rendered[i] == "\x1b":
            j = i + 1
            if j < len(rendered) and rendered[j] == "[":
                j += 1
                while j < len(rendered) and not ("@" <= rendered[j] <= "~"):
                    j += 1
            out.append("\001" + rendered[i:j + 1] + "\002")
            i = j + 1
        else:
            out.append(rendered[i])
            i += 1
    return "".join(out)


class PromptSession:
    """Reads lines for the REPL.

    ``read()`` returns the entered line, or ``None`` when the session should
    end (Ctrl+D, or a second consecutive Ctrl+C at the prompt).
    """

    EXIT_HINT = "[dim](press Ctrl+C again or type /quit to exit)[/dim]"

    def __init__(self, console: Console, project_root: str | os.PathLike,
                 prompt: str = "[bold green]> [/bold green]",
                 input_func: Optional[Callable[[str], str]] = None):
        self.console = console
        self.prompt = prompt
        self.history_file = history_path(project_root)
        self._pending_interrupt = False
        self._input_func = input_func
        self.readline_enabled = False
        if input_func is None and _readline is not None and _is_tty():
            self.readline_enabled = self._setup_readline()

    # -- readline ---------------------------------------------------------
    def _setup_readline(self) -> bool:
        try:
            _readline.set_history_length(HISTORY_LENGTH)
            with contextlib.suppress(OSError):
                if self.history_file.is_file():
                    _readline.read_history_file(str(self.history_file))
            _readline.set_completer(self._completer)
            _readline.set_completer_delims(" \t\n")
            if "libedit" in (getattr(_readline, "__doc__", "") or ""):
                _readline.parse_and_bind("bind ^I rl_complete")
            else:
                _readline.parse_and_bind("tab: complete")
            return True
        except Exception:
            return False

    def _completer(self, text: str, state: int) -> Optional[str]:
        try:
            line = _readline.get_line_buffer() if _readline else text
        except Exception:
            line = text
        # Only complete the command word itself, not its arguments.
        if line.lstrip() != text.lstrip() and " " in line.lstrip():
            return None
        matches = complete_command(text)
        return matches[state] if state < len(matches) else None

    def save_history(self) -> None:
        if not self.readline_enabled:
            return
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            # The history can hold anything typed at the prompt, so keep it
            # private and never write it through a symlink the repo planted.
            if self.history_file.is_symlink() or self.history_file.parent.is_symlink():
                return
            _readline.write_history_file(str(self.history_file))
            os.chmod(self.history_file, 0o600)
        except Exception:
            pass  # read-only checkout, permissions, etc. -- history is optional.

    # -- input ------------------------------------------------------------
    def _raw_input(self) -> str:
        if self._input_func is not None:
            return self._input_func(self.prompt)
        if self.readline_enabled:
            return input(_ansi_prompt(self.console, self.prompt))
        return self.console.input(self.prompt)

    def read(self) -> Optional[str]:
        while True:
            try:
                line = self._raw_input()
            except EOFError:
                self.console.print()
                return None
            except KeyboardInterrupt:
                self.console.print()
                if self._pending_interrupt:
                    return None
                self._pending_interrupt = True
                self.console.print(self.EXIT_HINT)
                continue
            self._pending_interrupt = False
            return line


def _is_tty() -> bool:
    try:
        import sys
        return sys.stdin.isatty()
    except Exception:
        return False


# === Startup banner =========================================================

def git_branch(project_root: str | os.PathLike) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"], cwd=str(project_root),
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    branch = out.stdout.strip() if out.returncode == 0 else ""
    return branch or None


def configured_model(ctx_obj: dict) -> Optional[str]:
    """Best-effort description of the model the coder role will use."""
    if ctx_obj.get("model"):
        return f"{ctx_obj['model']} (--model)"
    try:
        from local_coder.orchestrator.config_loader import load_config
        config = load_config(ctx_obj.get("config_path"), project_root=ctx_obj["project_root"])
        name = config.agentic.role_models.get("coder")
        if name not in config.models:
            name = next((n for n in ("coder", "default") if n in config.models), None)
        if name is None and config.models:
            name = next(iter(config.models))
        if name is None:
            return None
        model_id = config.models[name].model_id
        return f"{name} ({model_id})" if model_id and model_id != name else name
    except Exception:
        return None


def build_banner(ctx_obj: dict, version: str = "") -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column()
    title = Text.assemble(("Local Coding Agent", "bold cyan"), (f"  v{version}" if version else "", "dim"))
    grid.add_row(Text(""), title)
    grid.add_row("project", str(ctx_obj.get("project_root", "")))
    branch = git_branch(ctx_obj.get("project_root", "."))
    if branch:
        grid.add_row("branch", branch)
    model = configured_model(ctx_obj)
    grid.add_row("model", model or "[yellow]not configured[/yellow] (run local-coder init)")
    grid.add_row(
        "approval",
        "[bold red]--yolo: risky actions auto-approved[/bold red]" if ctx_obj.get("yolo") else "prompt before risky actions",
    )
    grid.add_row("", "[dim]/help for commands · Tab completes · Ctrl+C cancels · /quit exits[/dim]")
    return grid


# === Live progress ==========================================================

ROLE_STYLES = {
    "orchestrator": "cyan",
    "explorer": "green",
    "planner": "yellow",
    "coder": "blue",
    "debugger": "red",
    "tester": "magenta",
    "reviewer": "white",
    "security": "bright_red",
    "exploit_validator": "red",
}
ROLE_LABELS = {
    "orchestrator": "orch",
    "explorer": "explr",
    "planner": "plan",
    "coder": "code",
    "debugger": "debug",
    "tester": "test",
    "reviewer": "review",
    "security": "sec",
    "exploit_validator": "poc",
}
_LABEL_WIDTH = max(len(v) for v in ROLE_LABELS.values())


def role_of(source: str) -> str:
    """'CODER-1' -> 'coder'."""
    return (source or "").split("-")[0].split(":")[0].strip().lower() or "agent"


def role_label(source: str) -> str:
    role = role_of(source)
    base = ROLE_LABELS.get(role, role[:_LABEL_WIDTH])
    suffix = source.split("-", 1)[1] if "-" in (source or "") else ""
    return f"{base}{suffix}"


def format_elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def format_event(event) -> Text:
    """Compact, role-coloured one-line rendering of an AgentEvent."""
    role = role_of(event.source)
    style = ROLE_STYLES.get(role, "white")
    ts = event.timestamp.strftime("%H:%M:%S") if getattr(event, "timestamp", None) else ""
    line = Text()
    line.append(f"{ts} ", style="dim")
    line.append(f"{role_label(event.source):<{_LABEL_WIDTH}}", style=f"bold {style}")
    line.append(" │ ", style="dim")
    event_type = getattr(event, "event_type", "") or ""
    msg_style = "red" if ("error" in event_type or "failed" in event_type) else ""
    line.append(str(event.message), style=msg_style)
    return line


class _StatusText:
    """Renderable re-evaluated on every spinner refresh so elapsed time ticks."""

    def __init__(self, renderer: "ProgressRenderer"):
        self.renderer = renderer

    def __rich__(self) -> Text:
        return self.renderer.status_text()


class ProgressRenderer:
    """Spinner + event log for one running request.

    States: idle -> running -> (paused <-> running) -> done.
    """

    def __init__(self, console: Console, label: str, clock: Callable[[], float] = time.monotonic):
        self.console = console
        self.label = label
        self.clock = clock
        self.state = "idle"
        self.role: str = "orchestrator"
        self.activity: str = "starting"
        self.events_seen = 0
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self._status = None

    # -- state ------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else self.clock()
        return end - self.started_at

    def status_text(self) -> Text:
        style = ROLE_STYLES.get(self.role, "white")
        text = Text()
        text.append(self.role, style=f"bold {style}")
        activity = self.activity if len(self.activity) <= 60 else self.activity[:57] + "..."
        text.append(f"  {activity}", style="")
        text.append(f"  {format_elapsed(self.elapsed)}", style="dim")
        return text

    def start(self) -> None:
        if self.state != "idle":
            return
        self.started_at = self.clock()
        self.state = "running"
        self._start_spinner()

    def handle(self, event) -> None:
        self.events_seen += 1
        self.role = role_of(event.source)
        self.activity = str(event.message).splitlines()[0] if event.message else ""
        self.console.print(format_event(event), highlight=False)

    def pause(self) -> None:
        if self.state == "running":
            self._stop_spinner()
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "running"
            self._start_spinner()

    def finish(self, outcome: str = "done") -> None:
        """outcome: 'done', 'failed' or 'cancelled'."""
        if self.state == "done":
            return
        self._stop_spinner()
        self.finished_at = self.clock()
        self.state = "done"
        self.outcome = outcome
        self.console.print(self.footer(outcome))

    def footer(self, outcome: str) -> Text:
        mark, style = {
            "done": ("✓", "green"),
            "failed": ("✗", "red"),
            "cancelled": ("⏹", "yellow"),
        }.get(outcome, ("•", "white"))
        text = Text()
        text.append(f"{mark} {self.label} {outcome}", style=f"bold {style}")
        text.append(f" in {format_elapsed(self.elapsed)}", style="dim")
        if self.events_seen:
            text.append(f" · {self.events_seen} events", style="dim")
        return text

    # -- spinner ----------------------------------------------------------
    def _start_spinner(self) -> None:
        if not self.console.is_terminal:
            return
        try:
            self._status = self.console.status(_StatusText(self), spinner="dots")
            self._status.start()
        except Exception:
            self._status = None

    def _stop_spinner(self) -> None:
        if self._status is not None:
            with contextlib.suppress(Exception):
                self._status.stop()
            self._status = None


_ACTIVE: Optional[ProgressRenderer] = None


def active_renderer() -> Optional[ProgressRenderer]:
    return _ACTIVE


@contextlib.contextmanager
def progress(console: Console, label: str) -> Iterator[ProgressRenderer]:
    """Show a live spinner for the duration of the block and print a footer.

    KeyboardInterrupt / cancellation is reported as 'cancelled', any other
    exception as 'failed'; the exception still propagates.
    """
    global _ACTIVE
    renderer = ProgressRenderer(console, label)
    previous, _ACTIVE = _ACTIVE, renderer
    renderer.start()
    outcome = "failed"
    try:
        yield renderer
        outcome = "done"
    except BaseException as exc:
        outcome = "cancelled" if is_cancellation(exc) else "failed"
        raise
    finally:
        renderer.finish(outcome)
        _ACTIVE = previous


def is_cancellation(exc: BaseException) -> bool:
    """Ctrl+C in any of the shapes it reaches us: a raw KeyboardInterrupt,
    asyncio's CancelledError, or click.Abort (from click.confirm)."""
    import asyncio
    import click
    return isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError, click.Abort))


@contextlib.contextmanager
def paused() -> Iterator[None]:
    """Pause the active spinner (if any), e.g. while prompting for input."""
    renderer = _ACTIVE
    if renderer is not None:
        renderer.pause()
    history_len = _history_length()
    try:
        yield
    finally:
        _truncate_history(history_len)  # keep "y"/"n" answers out of history
    # Not in `finally`: on Ctrl+C the request is ending, and restarting the
    # spinner would draw it onto the half-finished prompt line.
    if renderer is not None:
        renderer.resume()


def _history_length() -> Optional[int]:
    if _readline is None:
        return None
    try:
        return _readline.get_current_history_length()
    except Exception:
        return None


def _truncate_history(length: Optional[int]) -> None:
    if length is None or _readline is None:
        return
    with contextlib.suppress(Exception):
        while _readline.get_current_history_length() > length:
            _readline.remove_history_item(_readline.get_current_history_length() - 1)


def render_event(console: Console, event) -> None:
    """Route an event to the active renderer, or print it plainly."""
    if _ACTIVE is not None:
        _ACTIVE.handle(event)
    else:
        console.print(format_event(event), highlight=False)


@contextlib.contextmanager
def sigint_raises() -> Iterator[None]:
    """Make Ctrl+C raise KeyboardInterrupt immediately inside the block.

    ``asyncio.run`` installs a SIGINT handler that only cancels the main
    task on the first Ctrl+C, which does nothing while a blocking prompt
    (the approval y/n) is waiting for input -- the user would have to press
    Ctrl+C twice. Temporarily restoring the default handler fixes that.
    """
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):  # pragma: no cover - exotic embedding
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal.SIGINT, previous)

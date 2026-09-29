"""User-defined slash commands loaded from Markdown prompt files.

A command is one ``.md`` file. Its path under a commands directory becomes
its name: ``review-auth.md`` is ``/review-auth`` and ``sec/triage.md`` is
``/sec:triage``. Two directories are searched, the project's own first:

    <project>/.local-coder/commands/
    ~/.config/local-coder/commands/   ($LOCAL_CODER_HOME/commands if set)

A project command wins over a personal one with the same name. The file is
an optional YAML front-matter block followed by the prompt template::

    ---
    description: Review the auth module for injection bugs
    argument-hint: <path>
    mode: run            # run (default) or plan
    ---
    Review $ARGUMENTS for SQL and command injection. Start with $1.

``$ARGUMENTS`` is everything typed after the command, ``$1``..``$9`` are
its whitespace-separated words (quotes group words). A template that uses
neither gets the arguments appended on a new line, so ``/explain foo.py``
still passes ``foo.py`` along.

Expansion is plain text substitution; nothing in a command file is executed.
The expanded prompt goes through the same agent run and approval gates as a
request typed at the prompt.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

MODES = ("run", "plan")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*(:[a-z0-9][a-z0-9_.-]*)*$")
_PLACEHOLDER_RE = re.compile(r"\$ARGUMENTS|\$([1-9])")
MAX_FILE_BYTES = 64 * 1024


@dataclass(frozen=True)
class CustomCommand:
    name: str            # with leading slash, e.g. "/sec:triage"
    template: str
    path: Path
    scope: str           # "project" or "user"
    description: str = ""
    argument_hint: str = ""
    mode: str = "run"

    def expand(self, arguments: str = "") -> str:
        """The prompt this command sends for the given argument string."""
        arguments = arguments.strip()
        try:
            words = shlex.split(arguments)
        except ValueError:  # unbalanced quote: fall back to plain split
            words = arguments.split()

        def substitute(match: re.Match) -> str:
            if match.group(1) is None:
                return arguments
            index = int(match.group(1)) - 1
            return words[index] if index < len(words) else ""

        # One pass, so a "$1" inside the typed arguments is never re-expanded.
        text, count = _PLACEHOLDER_RE.subn(substitute, self.template)
        if arguments and not count:
            text = f"{text.rstrip()}\n\n{arguments}"
        return text.strip()


def user_commands_dir() -> Path:
    home = os.environ.get("LOCAL_CODER_HOME")
    base = Path(home) if home else Path.home() / ".config" / "local-coder"
    return base / "commands"


def project_commands_dir(project_root: str | os.PathLike) -> Path:
    return Path(project_root) / ".local-coder" / "commands"


def _split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("".join(lines[1:i])) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), "".join(lines[i + 1:])
    return {}, text


def command_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return "/" + ":".join(part.lower() for part in rel.parts)


def load_command(path: Path, root: Path, scope: str) -> Optional[CustomCommand]:
    """Parse one command file, or None if it is unusable."""
    name = command_name(path, root)
    if not _NAME_RE.match(name[1:]):
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = _split_front_matter(text)
    if not body.strip():
        return None
    mode = str(meta.get("mode", "run")).strip().lower()
    return CustomCommand(
        name=name,
        template=body.strip(),
        path=path,
        scope=scope,
        description=str(meta.get("description") or "").strip() or _first_line(body),
        argument_hint=str(meta.get("argument-hint") or meta.get("argument_hint") or "").strip(),
        mode=mode if mode in MODES else "run",
    )


def _first_line(body: str) -> str:
    line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return line if len(line) <= 60 else line[:57] + "..."


def _scan(root: Path, scope: str) -> dict[str, CustomCommand]:
    found: dict[str, CustomCommand] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        command = load_command(path, root, scope)
        if command is not None:
            found[command.name] = command
    return found


def discover(project_root: str | os.PathLike, reserved: frozenset[str] | set[str] = frozenset()) -> tuple[list[CustomCommand], list[str]]:
    """Load custom commands for a project.

    Returns (commands sorted by name, warnings). Names in ``reserved``
    (the built-in commands) are skipped with a warning so a command file
    can never change what ``/quit`` or ``/rollback`` does.
    """
    commands = _scan(user_commands_dir(), "user")
    commands.update(_scan(project_commands_dir(project_root), "project"))  # project wins
    warnings = []
    for name in sorted(set(commands) & set(reserved)):
        warnings.append(f"{commands[name].path}: {name} is a built-in command, so this file is ignored")
        del commands[name]
    return [commands[n] for n in sorted(commands)], warnings


def split_invocation(line: str) -> tuple[str, str]:
    """'/fix-issue 42 fast' -> ('/fix-issue', '42 fast')."""
    head, _, rest = line.strip().partition(" ")
    return head.lower(), rest.strip()

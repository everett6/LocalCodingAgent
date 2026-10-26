"""Parse-check files right after an agent edits them.

A small model that breaks a file's syntax usually doesn't notice until a
test run fails several turns later, with an error far from the edit. This
catches it in the same turn, in-process and without any external linter:
the edit's tool result carries the parse error and the lines around it.
"""
from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import yaml

CHECKED_SUFFIXES = {".py", ".json", ".toml", ".yaml", ".yml"}
MAX_CHECK_BYTES = 2_000_000


def check_syntax(path: Path, display_path: str | None = None) -> str | None:
    """Return a description of the parse error in path, or None if it parses
    (or isn't a kind of file this checks)."""
    suffix = path.suffix.lower()
    if suffix not in CHECKED_SUFFIXES or not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_CHECK_BYTES:
            return None
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    name = display_path or str(path)
    line: int | None = None
    column: int | None = None
    try:
        if suffix == ".py":
            ast.parse(source, filename=name)
        elif suffix == ".json":
            json.loads(source)
        elif suffix == ".toml":
            tomllib.loads(source)
        else:
            list(yaml.safe_load_all(source))
        return None
    except SyntaxError as exc:
        message, line, column = exc.msg, exc.lineno, exc.offset
    except json.JSONDecodeError as exc:
        message, line, column = exc.msg, exc.lineno, exc.colno
    except tomllib.TOMLDecodeError as exc:
        message = str(exc)
        line = getattr(exc, "lineno", None)
        column = getattr(exc, "colno", None)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        message = getattr(exc, "problem", None) or str(exc)
        if mark is not None:
            line, column = mark.line + 1, mark.column + 1
    except (ValueError, RecursionError, MemoryError) as exc:
        message = str(exc)

    location = f"{name}:{line}" if line else name
    report = [f"{location}: {message}"]
    if line:
        report.append(_excerpt(source, line, column))
    return "\n".join(report)


def _excerpt(source: str, line: int, column: int | None, radius: int = 2) -> str:
    lines = source.splitlines()
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    width = len(str(end))
    out = []
    for number in range(start, end + 1):
        marker = ">" if number == line else " "
        out.append(f"{marker} {number:>{width}} | {lines[number - 1]}")
        if number == line and column:
            out.append(" " * (width + 5 + column - 1) + "^")
    return "\n".join(out)

"""Machine-readable (--json) output for CLI commands.

Every command run with --json prints exactly one JSON document on stdout:

    {
      "schema_version": 1,
      "command": "status",
      "ok": true,
      "data": {...},
      "error": null
    }

``data`` is command-specific (see docs/json-output.md); ``error`` is
``{"type": ..., "message": ...}`` when the command itself failed, and the
process exits 1 whenever ``ok`` is false. Human-readable output (panels,
agent events, prompts) goes to stderr in JSON mode so stdout stays
parseable.

Bump SCHEMA_VERSION only for breaking changes (a field removed, renamed,
or retyped); adding a field is not breaking.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import click
from rich.console import Console

SCHEMA_VERSION = 1

# The CLI's shared console. In JSON mode it is pointed at stderr.
console = Console()


def json_mode(ctx_obj: dict | None) -> bool:
    return bool(ctx_obj and ctx_obj.get("json"))


def envelope(command: str, ok: bool, data: Any = None, error: dict | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": ok,
        "data": data,
        "error": error,
    }


def error_info(exc: BaseException) -> dict:
    message = exc.format_message() if isinstance(exc, click.ClickException) else str(exc)
    return {"type": type(exc).__name__, "message": message}


def write_json(document: dict) -> None:
    click.echo(json.dumps(document, indent=2, default=str))


def emit(ctx_obj: dict, data: Any, ok: bool = True) -> None:
    """Print a command's result envelope and exit 1 if it did not succeed."""
    write_json(envelope(ctx_obj.get("command", ""), ok, data))
    if not ok:
        sys.exit(1)


def _set_json(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    ctx.ensure_object(dict)
    if value:
        ctx.obj["json"] = True
        console.stderr = True
    # "local-coder local-server status" -> "local-server status"
    ctx.obj["command"] = " ".join(ctx.command_path.split()[1:])


json_option = click.option(
    "--json", "json_output", is_flag=True, expose_value=False, callback=_set_json,
    help="Print a machine-readable JSON result on stdout.",
)


class JsonAwareGroup(click.Group):
    """Reports a subcommand's failure as a JSON error envelope in JSON mode."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (click.exceptions.Exit, click.Abort, SystemExit):
            raise
        except Exception as exc:
            if not json_mode(ctx.obj):
                raise
            write_json(envelope(ctx.obj.get("command", ""), False, None, error_info(exc)))
            ctx.exit(1)

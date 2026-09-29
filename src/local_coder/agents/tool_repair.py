"""Recover tool calls that a small local model got slightly wrong.

Frontier models almost always emit a well-formed native tool call. Small
quantized models often don't: they write the call as text in the message
body (Hermes/Qwen ``<tool_call>`` tags, a fenced JSON block), emit
arguments that are Python-ish rather than JSON (single quotes, ``True``,
a trailing comma, a missing closing brace), call ``Read`` instead of
``read_file``, say ``file_path`` where the schema says ``path``, or pass
``"true"`` for a boolean. Each of those used to cost a whole turn -- or
worse, invalid JSON silently became empty arguments.

This module fixes what can be fixed unambiguously and says so, so the
model sees what was interpreted and learns the right shape; anything it
can't fix becomes a clear error message instead of a silent guess.
"""
from __future__ import annotations

import ast
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from local_coder.types import ToolCall

# A backend that received tool-call arguments it couldn't repair stores the
# raw text under this key instead of pretending the call had no arguments.
RAW_ARGUMENTS_KEY = "__raw_arguments__"

# Names other agents' tools use for the same thing. Only consulted when the
# alias target is actually one of the caller's tools.
TOOL_NAME_ALIASES = {
    "read": "read_file",
    "view": "read_file",
    "cat": "read_file",
    "open_file": "read_file",
    "write": "write_file",
    "create_file": "write_file",
    "edit": "edit_file",
    "str_replace": "edit_file",
    "replace_in_file": "edit_file",
    "patch": "apply_patch",
    "ls": "list_files",
    "list_dir": "list_files",
    "list_directory": "list_files",
    "glob": "search_files",
    "find_files": "search_files",
    "search": "grep",
    "rg": "grep",
    "bash": "run_command",
    "shell": "run_command",
    "execute_command": "run_command",
    "run_shell_command": "run_command",
    "test": "run_tests",
    "pytest": "run_tests",
}

# Argument names models commonly use for a schema property, keyed by the
# schema's name. Applied only when the schema has the canonical property
# and the call didn't already pass it.
ARGUMENT_ALIASES = {
    "path": ("file_path", "filepath", "filename", "file", "file_name", "target_file", "dir", "directory"),
    "content": ("contents", "text", "file_content", "data", "body"),
    "command": ("cmd", "shell_command", "script"),
    "pattern": ("query", "regex", "search", "glob"),
    "old_string": ("old_str", "old", "search", "find"),
    "new_string": ("new_str", "new", "replace", "replacement"),
    "patch": ("diff", "unified_diff"),
    "message": ("msg", "commit_message"),
}

_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)
_FUNCTION_TAG = re.compile(r"<function=([\w.-]+)>\s*(.*?)\s*</function>", re.DOTALL)
_FENCED_BLOCK = re.compile(r"^```(?:json|tool_call|tool)?\s*\n(.*?)\n?```$", re.DOTALL)


@dataclass
class RepairedCall:
    """A tool call after repair: either ready to run, or an error to report."""

    call: ToolCall
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def parse_arguments(text: str) -> dict[str, Any] | None:
    """Parse tool-call arguments leniently; None if nothing sensible parses."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not candidate:
        return {}
    fenced = _FENCED_BLOCK.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    for attempt in (candidate, _close_brackets(candidate)):
        for variant in (attempt, re.sub(r",\s*([}\]])", r"\1", attempt)):
            parsed = _loads(variant)
            if isinstance(parsed, dict):
                return parsed
    return None


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Python literal syntax: single quotes, True/False/None.
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _close_brackets(text: str) -> str:
    """Close brackets and a string left open by a generation that stopped
    early (a max_tokens cutoff mid-call)."""
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in text:
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    return text + (quote or "") + "".join(reversed(stack))


def extract_text_tool_calls(content: str, tool_names: set[str]) -> list[ToolCall]:
    """Find tool calls a model wrote as text instead of as native calls.

    Tagged formats (``<tool_call>{...}</tool_call>``, ``<function=name>{...}
    </function>``) are accepted anywhere in the message. A bare or fenced
    JSON object is accepted only when it is the whole message, so an answer
    that merely shows an example call isn't executed.
    """
    if not content or not tool_names:
        return []
    calls: list[ToolCall] = []
    for body in _TOOL_CALL_TAG.findall(content):
        call = _call_from_object(parse_arguments(body), tool_names)
        if call is not None:
            calls.append(call)
    for name, body in _FUNCTION_TAG.findall(content):
        arguments = parse_arguments(body)
        if resolve_tool_name(name, tool_names) is not None and arguments is not None:
            calls.append(ToolCall(name=name, arguments=arguments))
    if calls:
        return calls
    parsed = parse_arguments(content)
    if parsed is None:
        stripped = content.strip()
        if stripped.startswith("[") or _FENCED_BLOCK.match(stripped):
            inner = _FENCED_BLOCK.match(stripped)
            items = _loads(inner.group(1) if inner else stripped)
            if isinstance(items, list):
                found = [_call_from_object(item, tool_names) for item in items]
                if found and all(found):
                    return [call for call in found if call is not None]
        return []
    call = _call_from_object(parsed, tool_names)
    return [call] if call is not None else []


def _call_from_object(obj: Any, tool_names: set[str]) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not isinstance(name, str):
        return None
    if resolve_tool_name(name, tool_names) is None:
        return None
    arguments = obj.get("arguments", obj.get("parameters", obj.get("args", obj.get("input", {}))))
    if isinstance(arguments, str):
        arguments = parse_arguments(arguments)
    if not isinstance(arguments, dict):
        return None
    # The name stays as written; repair_tool_call renames it and says so.
    return ToolCall(name=name, arguments=arguments)


def resolve_tool_name(name: str, tool_names: set[str]) -> str | None:
    """Map a near-miss tool name onto one of tool_names, or None."""
    if name in tool_names:
        return name
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.strip())
    normalized = re.sub(r"[\s\-.]+", "_", normalized).lower()
    for prefix in ("functions_", "tools_", "tool_"):
        if normalized.startswith(prefix) and normalized[len(prefix):]:
            normalized = normalized[len(prefix):]
    if normalized in tool_names:
        return normalized
    alias = TOOL_NAME_ALIASES.get(normalized)
    if alias in tool_names:
        return alias
    close = difflib.get_close_matches(normalized, sorted(tool_names), n=2, cutoff=0.85)
    # Only a single clear winner; two close candidates is a guess.
    if len(close) == 1:
        return close[0]
    return None


def normalize_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Rename aliased keys and coerce scalar types to match a JSON schema."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return arguments, []
    notes: list[str] = []
    args = dict(arguments)

    # {"arguments": {...}} / {"params": {...}} wrapping a real argument set.
    if len(args) == 1:
        (only_key, only_value), = args.items()
        if only_key not in properties and only_key in {"arguments", "args", "params", "parameters", "input"} \
                and isinstance(only_value, dict):
            args = dict(only_value)
            notes.append(f"unwrapped arguments from '{only_key}'")

    for canonical, aliases in ARGUMENT_ALIASES.items():
        if canonical not in properties or canonical in args:
            continue
        for alias in aliases:
            if alias in args and alias not in properties:
                args[canonical] = args.pop(alias)
                notes.append(f"treated '{alias}' as '{canonical}'")
                break

    for key, value in list(args.items()):
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        coerced = _coerce(value, spec.get("type"))
        if coerced is not _NO_CHANGE:
            args[key] = coerced
            notes.append(f"converted '{key}' to {spec.get('type')}")
    return args, notes


_NO_CHANGE = object()


def _coerce(value: Any, expected: Any) -> Any:
    if expected == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    elif expected == "integer":
        if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
    elif expected == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    elif expected == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    elif expected == "array" and isinstance(value, str):
        parsed = _loads(value.strip())
        return parsed if isinstance(parsed, list) else [value]
    return _NO_CHANGE


def repair_tool_call(call: ToolCall, schemas: dict[str, dict[str, Any]]) -> RepairedCall:
    """Repair one call against the caller's tool schemas (name -> parameters).

    Names that can't be resolved pass through unchanged: the registry's own
    "unknown tool" error is the authoritative answer for those.
    """
    notes: list[str] = []
    name = call.name
    if schemas and name not in schemas:
        resolved = resolve_tool_name(name, set(schemas))
        if resolved is not None:
            notes.append(f"interpreted tool '{name}' as '{resolved}'")
            name = resolved

    arguments = dict(call.arguments)
    if RAW_ARGUMENTS_KEY in arguments:
        raw = str(arguments.pop(RAW_ARGUMENTS_KEY))
        preview = raw if len(raw) <= 300 else raw[:300] + "..."
        error = (
            f"The arguments for {name} were not valid JSON and could not be repaired: {preview}\n"
            "Send the call again with arguments as a single JSON object, e.g. "
            '{"path": "src/app.py"}. Use double quotes and escape newlines inside strings as \\n.'
        )
        return RepairedCall(ToolCall(id=call.id, name=name, arguments=arguments), error=error, notes=notes)

    schema = schemas.get(name) or {}
    arguments, arg_notes = normalize_arguments(arguments, schema)
    notes.extend(arg_notes)

    required = schema.get("required") if isinstance(schema, dict) else None
    missing = [key for key in (required or []) if key not in arguments]
    if missing:
        known = ", ".join(sorted((schema.get("properties") or {}).keys()))
        error = (
            f"{name} is missing required argument(s): {', '.join(missing)}. "
            f"Its arguments are: {known}."
        )
        return RepairedCall(ToolCall(id=call.id, name=name, arguments=arguments), error=error, notes=notes)

    return RepairedCall(ToolCall(id=call.id, name=name, arguments=arguments), notes=notes)


def schemas_by_name(tool_schemas: list[dict]) -> dict[str, dict[str, Any]]:
    """Index OpenAI-style function schemas by tool name."""
    indexed: dict[str, dict[str, Any]] = {}
    for schema in tool_schemas or []:
        function = schema.get("function", schema) if isinstance(schema, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            indexed[function["name"]] = function.get("parameters") or {}
    return indexed

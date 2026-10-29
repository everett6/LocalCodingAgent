"""Focused source-read and model-text privacy controls, not a DLP guarantee.

Known values come only from explicitly configured providers. Encoded/obfuscated
credentials, arbitrary hardlink aliases and opaque provider protocol fields are
outside this textual filter's contract.
"""
from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
import re
from typing import Any

from local_coder.context.source_snapshot import is_private_source

_PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
_PRIVATE_NAMES = {"secrets.json", "secrets.yaml", "secrets.yml"}
_KEY_MARKER = re.compile(r"-----(BEGIN|END) [^-\r\n]{0,80}PRIVATE KEY-----")
_KEY_MATERIAL = re.compile(r"[A-Za-z0-9+/=]{16,}\Z")
_TOKENS = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+)\b"
)
_AUTH = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
_SECRET_FIELD = r"(?:[A-Za-z][A-Za-z0-9]{0,31}[_-]){0,4}(?:password|passwd|secret|secret[_-]?key|token|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|credentials?|private[_-]?key|aws_secret_access_key)"
_ASSIGNMENT = re.compile(
    rf"(\b{_SECRET_FIELD}[\"']?\s*[:=]\s*)(\"[^\"\r\n]+\"|'[^'\r\n]+')", re.I
)
_SECRET_FIELD_NAME = re.compile(rf"{_SECRET_FIELD}\Z", re.I)
_URL_CREDENTIALS = re.compile(r"((?:https?|postgres(?:ql)?|mysql|redis)://)[^\s/@:]+:[^\s/@]+@", re.I)
REDACTION_NOTICE = "[Credential text redacted from this model-visible observation; source hashes refer to the original bytes. Exact-match edits of redacted text may fail.]"


def _private_relative(path: Path) -> bool:
    return (is_private_source(path) or path.name.lower() in _PRIVATE_NAMES
            or path.suffix.lower() in _PRIVATE_SUFFIXES
            or path.name.lower().startswith(("secrets.", "credentials.")))


def private_read_path(path: str | Path, project_root: str | Path | None = None) -> bool:
    """Check lexical and resolved paths; ignore private ancestors of the project.

    With no project root, callers must supply a relative path. This does not
    identify protected files reached through an unrelated hardlink name.
    """
    candidate = Path(str(path).replace("\\", "/"))
    if project_root is None:
        return _private_relative(candidate)
    try:
        root = Path(project_root).resolve()
        lexical = Path(os.path.abspath(root / candidate))
        relative = lexical.relative_to(root)
        if _private_relative(relative):
            return True
        return _private_relative(lexical.resolve().relative_to(root))
    except (OSError, ValueError, RuntimeError):
        return True


def bounded_known_secrets(values: Iterable[Any]) -> tuple[str, ...]:
    """Bind at most 64 configured values; never scan ambient environment state.

    Short values are excluded from exact replacement to avoid masking ordinary
    prose everywhere. Recognizable credential syntax is filtered independently.
    """
    selected = set()
    for value in values:
        if isinstance(value, str) and 8 <= len(value) <= 4096:
            selected.add(value)
            if len(selected) == 64:
                break
    return tuple(sorted(selected, key=lambda value: (-len(value), value)))


def _masked(match: re.Match) -> str:
    return "[redacted]" + "\n" * match.group().count("\n")


def _redact_private_keys(text: str) -> str:
    """Scan each line once; many unmatched BEGIN headers cannot rescan tails."""
    spans = []
    pending = partial = next_material = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip(" \t\r\n")
        if next_material is not None and partial is None and _KEY_MATERIAL.fullmatch(stripped):
            partial = next_material
        next_material = None
        for marker in _KEY_MARKER.finditer(line):
            if marker.group(1) == "BEGIN":
                if pending is None:
                    pending = offset + marker.start()
            elif pending is not None:
                spans.append((pending, offset + marker.end()))
                pending = partial = None
        standalone = _KEY_MARKER.fullmatch(stripped)
        if standalone is not None and standalone.group(1) == "BEGIN":
            next_material = offset + line.index("-----")
        offset += len(line)
    if partial is not None:
        spans.append((partial, len(text)))
    if not spans:
        return text
    parts, position = [], 0
    for start, end in spans:
        parts.extend((text[position:start], "[redacted]" + "\n" * text.count("\n", start, end)))
        position = end
    parts.append(text[position:])
    return "".join(parts)


def redact_model_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Redact plaintext credentials while retaining original line numbering."""
    text = _redact_private_keys(text)
    for secret in secrets:
        if isinstance(secret, str) and 8 <= len(secret) <= 4096:
            text = text.replace(secret, "[redacted]" + "\n" * secret.count("\n"))
    text = _TOKENS.sub(_masked, text)
    text = _AUTH.sub(_masked, text)

    def assignment(match):
        value = match.group(2)
        replacement = value[0] + "[redacted]" + value[0] if value[0] in {"\"", "'"} else "[redacted]"
        return match.group(1) + replacement

    text = _ASSIGNMENT.sub(assignment, text)
    return _URL_CREDENTIALS.sub(lambda match: match.group(1) + "[redacted]@", text)


def redact_diagnostic_value(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Copy JSON-shaped diagnostic data without raw credential fields."""
    if isinstance(value, str):
        return redact_model_text(value, secrets)
    if isinstance(value, dict):
        return {redact_model_text(str(key), secrets): "[redacted]" if _SECRET_FIELD_NAME.fullmatch(str(key))
                and item is not None and not isinstance(item, (int, float, bool))
                else redact_diagnostic_value(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_diagnostic_value(item, secrets) for item in value]
    return value


class ModelTextView:
    """Apply egress text filtering without replacing the host's model identity.

    Assistant messages, tool arguments, IDs and provider reasoning/signature
    fields are preserved byte-for-byte. Host system/user/tool text is copied.
    """
    def __init__(self, model, secrets: tuple[str, ...] = ()):
        self.model = model
        self.secrets = secrets

    def visible(self, messages):
        visible = []
        for message in messages:
            if message.role in {"system", "user", "tool"}:
                content = redact_model_text(message.content, self.secrets)
                message = message.model_copy(update={"content": content}) if content != message.content else message
            visible.append(message)
        return visible

    async def generate(self, messages, **kwargs):
        return await self.model.generate(self.visible(messages), **kwargs)

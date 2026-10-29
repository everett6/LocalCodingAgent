"""Private saved provider configurations; credentials remain in environment refs."""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from local_coder.memory.context_store import _clean
from local_coder.statedir import state_path
from local_coder.types import ModelBackend, ModelConfig

PROFILE_PATH = ".local-coder/providers/profiles.json"
MAX_BYTES = 128000
_ID = re.compile(r"[a-f0-9]{32}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid provider profile ID")
    return value


def _secret_values(provider):
    """Recognize supplied credentials, including values nested in dropped fields."""
    values = []

    def visit(value, secret=False):
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
            secret = True
        if isinstance(value, dict):
            for key, item in value.items():
                sensitive = (
                    isinstance(key, str)
                    and key != "api_key_env"
                    and bool(
                        re.search(
                            r"key|token|password|authorization|credential|secret",
                            key,
                            re.IGNORECASE,
                        )
                    )
                )
                visit(item, secret or sensitive)
        elif isinstance(value, list):
            for item in value:
                visit(item, secret)
        elif secret and isinstance(value, str) and value:
            values.append(value)

    visit(provider)
    source = provider.get("api_key_env")
    if isinstance(source, str) and _ENV.fullmatch(source) and os.environ.get(source):
        values.append(os.environ[source])
    return tuple(values)


def _safe_text(value, maximum, secrets=(), *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value)
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(
            "Profile text must be bounded and contain no control characters"
        )
    decoded = unquote(value)
    if _clean(value) != value or any(
        secret == value or (len(secret) >= 4 and secret in decoded)
        for secret in secrets
    ):
        raise ValueError("Credential values cannot be saved in a provider profile")
    return value


def _provider(raw):
    if isinstance(raw, ModelConfig):
        value = raw.model_dump(mode="json")
        if raw.api_key is not None:
            value["api_key"] = raw.api_key
        raw = value
    if not isinstance(raw, dict) or not {"backend", "model_id", "base_url"} <= set(raw):
        raise ValueError("A provider profile requires protocol, model and endpoint")
    secrets = _secret_values(raw)
    allowed = set(ModelConfig.model_fields) - {"api_key"}
    fields = {key: value for key, value in raw.items() if key in allowed}
    if fields.get("api_key_env") == "":
        fields["api_key_env"] = None
    for key in ("auto_context", "context_source", "request_cost_usd"):
        fields.pop(key, None)
    fields.setdefault("name", "saved-provider")
    try:
        fields["backend"] = ModelBackend(fields["backend"])
        validated = ModelConfig.model_validate(fields, strict=True)
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Invalid provider profile configuration") from None
    clean = validated.model_dump(mode="json")
    clean.pop("api_key", None)
    clean["name"] = _safe_text(clean["name"], 100, secrets)
    clean["model_id"] = _safe_text(clean["model_id"], 512, secrets)
    clean["base_url"] = _safe_text(clean["base_url"], 2048, secrets)
    parsed = urlsplit(clean["base_url"])
    try:
        loopback = (
            parsed.hostname == "localhost"
            or ipaddress.ip_address(parsed.hostname or "").is_loopback
        )
    except ValueError:
        loopback = False
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid provider endpoint port") from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
    ):
        raise ValueError(
            "Provider endpoints require HTTPS or explicit loopback HTTP without embedded credentials"
        )
    env = clean.get("api_key_env")
    if env is not None and (not isinstance(env, str) or not _ENV.fullmatch(env)):
        raise ValueError("Credential references must be environment variable names")
    for key in ("context_length", "max_tokens"):
        if type(clean[key]) is not int or not 1 <= clean[key] <= 1_000_000_000:
            raise ValueError("Provider token limits must be positive bounded integers")
    for key in ("input_cost_per_million", "output_cost_per_million"):
        if clean.get(key) is not None and (
            not math.isfinite(clean[key]) or clean[key] < 0
        ):
            raise ValueError("Provider prices must be finite and nonnegative")
    auto = raw.get("auto_context", True)
    if type(auto) is not bool:
        raise ValueError("auto_context must be a boolean")
    source = _safe_text(raw.get("context_source", "configured"), 100, secrets)
    request_cost = raw.get("request_cost_usd", 0)
    if request_cost is not None and (
        isinstance(request_cost, bool)
        or not isinstance(request_cost, (int, float))
        or not math.isfinite(request_cost)
        or request_cost < 0
    ):
        raise ValueError("Per-request cost must be finite and nonnegative")
    for key, value in clean.items():
        if isinstance(value, str) and key != "api_key_env":
            _safe_text(value, 2048, secrets, empty=True)
    clean.update(
        auto_context=auto, context_source=source, request_cost_usd=request_cost
    )
    return clean, secrets


class ProviderProfileStore:
    def __init__(self, project_root):
        self.project_root = Path(project_root).resolve()
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(
                str(self.project_root / PROFILE_PATH), threading.RLock()
            )

    def _path(self):
        target = state_path(self.project_root, PROFILE_PATH)
        target.parent.chmod(0o700)
        if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
            raise ValueError("Provider profiles must be ordinary private files")
        return target

    def _read(self):
        target = self._path()
        if not target.exists():
            return []
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_nlink != 1 or info.st_size > MAX_BYTES:
                raise ValueError(
                    "Provider profile storage exceeds limits or uses links"
                )
            data = stream.read(MAX_BYTES + 1)
        try:
            saved = json.loads(data)
            records = saved.get("profiles") if isinstance(saved, dict) else None
            if not isinstance(records, list) or len(records) > 64:
                raise ValueError
            result, identities = [], set()
            for record in records:
                if not isinstance(record, dict):
                    raise TypeError
                identity = _identity(record.get("id"))
                if identity in identities:
                    raise ValueError
                identities.add(identity)
                provider, secrets = _provider(record.get("provider"))
                name = _safe_text(record.get("name"), 80, secrets)
                updated = _safe_text(record.get("updated_at"), 64)
                if datetime.fromisoformat(updated).tzinfo is None:
                    raise ValueError
                result.append(
                    {
                        "id": identity,
                        "name": name,
                        "provider": provider,
                        "updated_at": updated,
                    }
                )
            return result
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("Invalid provider profile storage") from None

    def _write(self, records):
        encoded = json.dumps(
            {"version": 1, "profiles": records}, ensure_ascii=False, allow_nan=False
        ).encode()
        if len(encoded) > MAX_BYTES:
            raise ValueError("Provider profile storage exceeds 128 KB")
        target = self._path()
        descriptor, temporary = tempfile.mkstemp(
            prefix=".profiles-", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            self._path()
            os.replace(temporary, target)
            target.chmod(0o600)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def list(self):
        with self._lock:
            return copy.deepcopy(self._read())

    def get(self, id):
        identity = _identity(id)
        with self._lock:
            record = next(
                (record for record in self._read() if record["id"] == identity), None
            )
            if record is None:
                raise ValueError("Provider profile does not exist")
            return copy.deepcopy(record)

    def save(self, name, provider, id=None):
        normalized, secrets = _provider(provider)
        name = _safe_text(name, 80, secrets)
        identity = _identity(id) if id is not None else uuid.uuid4().hex
        with self._lock:
            records = self._read()
            previous = next(
                (
                    index
                    for index, record in enumerate(records)
                    if record["id"] == identity
                ),
                None,
            )
            if id is not None and previous is None:
                raise ValueError("Provider profile does not exist")
            if previous is None and len(records) >= 64:
                raise ValueError("At most 64 provider profiles may be saved")
            record = {
                "id": identity,
                "name": name,
                "provider": normalized,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if previous is None:
                records.append(record)
            else:
                records[previous] = record
            self._write(records)
            return copy.deepcopy(record)

    def delete(self, id):
        identity = _identity(id)
        with self._lock:
            records = self._read()
            kept = [record for record in records if record["id"] != identity]
            if len(kept) == len(records):
                return False
            self._write(kept)
            return True

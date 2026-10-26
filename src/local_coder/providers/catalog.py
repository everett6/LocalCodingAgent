"""Read-only official provider metadata with bounded, private catalog caching.

The public OpenRouter list is fetched without credentials. Key quota metadata
is requested only for the explicitly configured official provider and is never
cached. Local probes contact only one explicitly selected loopback endpoint.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from local_coder.integrations.mcp import redact_data
from local_coder.statedir import state_path, write_state_file

MODELS_URL = "https://openrouter.ai/api/v1/models"
KEY_URL = "https://openrouter.ai/api/v1/key"
CACHE_PATH = ".local-coder/providers/openrouter-models.json"
MAX_MODELS = 50000
MAX_RESPONSE_BYTES = 32_000_000
_MODEL_ID = re.compile(r"[A-Za-z0-9~][A-Za-z0-9._:/@+~ -]{0,511}\Z")


def _get(config, key, default=None):
    return (
        config.get(key, default)
        if isinstance(config, dict)
        else getattr(config, key, default)
    )


def _token(config):
    resolver = getattr(config, "resolve_api_key", None)
    token = resolver() if callable(resolver) else _get(config, "api_key")
    if (
        token is None
        and isinstance(config, dict)
        and isinstance(config.get("api_key_env"), str)
    ):
        token = os.environ.get(config["api_key_env"])
    if hasattr(token, "get_secret_value"):
        token = token.get_secret_value()
    if token is None:
        return None
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 4096
        or any(not 33 <= ord(char) <= 126 for char in token)
    ):
        raise ValueError("Invalid configured provider credential")
    return token


def is_openrouter(config) -> bool:
    """Accept only the official HTTPS base, without URL-embedded credentials."""
    value = _get(config, "base_url", "")
    if not isinstance(value, str) or any(ord(char) < 33 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "openrouter.ai"
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path.rstrip("/") == "/api/v1"
        )
    except ValueError:
        return False


def _integer(value, *, maximum=1_000_000_000):
    return value if type(value) is int and 0 < value <= maximum else None


def _money(value, *, allow_negative=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        amount = Decimal(str(value))
        if (
            not amount.is_finite()
            or (amount < 0 and not allow_negative)
            or abs(amount) > 1_000_000_000
        ):
            return None
        converted = float(amount)
        if amount != 0 and converted == 0:
            return None
        return converted if math.isfinite(converted) else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _profile(identity):
    name = identity.lower()
    if "deepseek" in name:
        return "openrouter_deepseek"
    if "glm" in name:
        return "openrouter_glm"
    if "kimi" in name:
        return "openrouter_kimi"
    if "qwen" in name:
        return "qwen38"
    return "generic"


def _normalize(row, provider="openrouter"):
    if not isinstance(row, dict):
        raise TypeError("Invalid model record")
    identity = row.get("id")
    name = row.get("name", identity)
    if (
        not isinstance(identity, str)
        or not 1 <= len(identity) <= 512
        or any(ord(char) < 32 for char in identity)
        or (provider != "local" and not _MODEL_ID.fullmatch(identity))
        or redact_data(identity) != identity
    ):
        raise ValueError("Invalid model ID")
    if (
        not isinstance(name, str)
        or len(name) > 512
        or any(ord(char) < 32 for char in name)
        or redact_data(name) != name
    ):
        raise ValueError("Invalid model name")
    supported = row.get("supported_parameters", [])
    if (
        not isinstance(supported, list)
        or len(supported) > 128
        or any(
            not isinstance(value, str)
            or len(value) > 100
            or not re.fullmatch(r"[a-zA-Z0-9_]+", value)
            for value in supported
        )
    ):
        raise ValueError("Invalid supported parameter metadata")
    pricing = row.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    if len(pricing) > 64 or any(
        not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,100}", key)
        for key in pricing
    ):
        raise ValueError("Pricing metadata exceeds limit")
    rates = {_key: _money(value) for _key, value in pricing.items()}
    prompt, completion = rates.get("prompt"), rates.get("completion")
    # Optional fees omitted by the official API are not charged. Missing token
    # prices and any supplied-but-invalid auxiliary fee remain unknown.
    request = rates.get("request", 0.0)
    complete = (
        prompt is not None
        and completion is not None
        and all(value is not None for value in rates.values())
    )
    free = (
        complete
        and prompt == 0
        and completion == 0
        and all(value == 0 for value in rates.values())
    )
    top = row.get("top_provider")
    top = top if isinstance(top, dict) else {}
    context = _integer(row.get("context_length")) or _integer(top.get("context_length"))
    return {
        "id": identity,
        "name": name,
        "context_length": context,
        "max_output_tokens": _integer(top.get("max_completion_tokens")),
        "input_cost_per_million": prompt * 1_000_000 if prompt is not None else None,
        "output_cost_per_million": completion * 1_000_000
        if completion is not None
        else None,
        "request_cost_usd": request,
        "is_free": bool(free),
        "free_variant": identity.endswith(":free"),
        "supports_tools": "tools" in supported,
        "supported_parameters": list(supported),
        "provider": provider,
        "profile": _profile(identity),
        "pricing_complete": complete,
    }


def _catalog(payload):
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("data"), list)
        or len(payload["data"]) > MAX_MODELS
    ):
        raise ValueError("Invalid or oversized provider model catalog")
    rows = payload["data"]
    if "total_count" in payload and (
        type(payload["total_count"]) is not int or payload["total_count"] != len(rows)
    ):
        raise ValueError("Provider returned an incomplete catalog")
    models = [_normalize(row) for row in rows]
    if len({item["id"] for item in models}) != len(models):
        raise ValueError("Provider returned duplicate model IDs")
    return models


class CatalogService:
    def __init__(
        self,
        project_root,
        *,
        transport=None,
        client=None,
        ttl_seconds=900,
        clock=time.time,
    ):
        if client is not None and transport is not None:
            raise ValueError("Supply either a client or a transport")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not 0 <= ttl_seconds <= 86400
        ):
            raise ValueError("Catalog TTL must be between zero and one day")
        self.project_root = Path(project_root).resolve()
        self.transport, self.client = transport, client
        self.ttl_seconds, self.clock = ttl_seconds, clock
        self._lock = threading.RLock()

    @contextmanager
    def _client(self):
        if self.client is not None:
            yield self.client
        else:
            with httpx.Client(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=15,
            ) as client:
                yield client

    def _json(
        self,
        client,
        url,
        *,
        token=None,
        maximum=MAX_RESPONSE_BYTES,
        extensions=None,
        host=None,
    ):
        headers = {"Accept": "application/json"}
        if host:
            headers["Host"] = host
        if token:
            headers["Authorization"] = "Bearer " + token
        # Construct a fresh request to avoid inheriting an injected client's
        # default cookies/authorization, especially for the public catalog.
        request = httpx.Request(
            "GET",
            url,
            headers=headers,
            extensions={
                "timeout": {"connect": 5, "read": 15, "write": 5, "pool": 5},
                **(extensions or {}),
            },
        )
        response = client.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            if response.status_code != 200:
                raise ValueError("Provider metadata request failed")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > maximum):
                raise ValueError("Provider metadata exceeds response limit")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > maximum:
                    raise ValueError("Provider metadata exceeds response limit")
                chunks.append(chunk)
            value = json.loads(b"".join(chunks))
            if not isinstance(value, dict):
                raise TypeError("Provider metadata must be an object")
            return value
        finally:
            response.close()

    def _cache_path(self):
        target = state_path(self.project_root, CACHE_PATH)
        target.parent.chmod(0o700)
        if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
            raise ValueError("Provider cache must be an ordinary private file")
        return target

    def _read_cache(self, target):
        if not target.exists():
            return None
        if target.stat().st_size > MAX_RESPONSE_BYTES:
            return None
        try:
            cached = json.loads(target.read_text(encoding="utf-8"))
            if (
                not isinstance(cached, dict)
                or cached.get("version") != 1
                or not isinstance(cached.get("fetched_at"), str)
            ):
                return None
            stamp = datetime.fromisoformat(cached["fetched_at"])
            if stamp.tzinfo is None or stamp.timestamp() > self.clock() + 60:
                return None
            models = _catalog(cached)
            return {
                "models": models,
                "source": "cache",
                "fetched_at": cached["fetched_at"],
                "stale": self.clock() - stamp.timestamp() >= self.ttl_seconds,
            }
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return None

    def openrouter_models(self, refresh=False):
        if type(refresh) is not bool:
            raise ValueError("refresh must be a boolean")
        with self._lock:
            target = self._cache_path()
            cached = self._read_cache(target)
            if cached is not None and not refresh and not cached["stale"]:
                return cached
            try:
                with self._client() as client:
                    payload = self._json(client, MODELS_URL)
                models = _catalog(payload)
                fetched_at = datetime.fromtimestamp(self.clock(), UTC).isoformat()
                # Store only normalized public metadata needed to reconstruct
                # the catalog; descriptions, links and unknown response fields
                # never enter private state.
                safe_rows = [
                    {
                        "id": row["id"],
                        "name": row.get("name", row["id"]),
                        "context_length": row.get("context_length"),
                        "top_provider": {
                            key: (
                                row.get("top_provider")
                                if isinstance(row.get("top_provider"), dict)
                                else {}
                            ).get(key)
                            for key in ("context_length", "max_completion_tokens")
                        },
                        "pricing": {
                            key: _money(value)
                            for key, value in (
                                row.get("pricing")
                                if isinstance(row.get("pricing"), dict)
                                else {}
                            ).items()
                        },
                        "supported_parameters": row.get("supported_parameters", []),
                    }
                    for row in payload["data"]
                ]
                encoded = json.dumps(
                    {"version": 1, "fetched_at": fetched_at, "data": safe_rows},
                    allow_nan=False,
                )
                if len(encoded.encode()) > MAX_RESPONSE_BYTES:
                    raise ValueError("Provider cache exceeds size limit")
                write_state_file(self.project_root, CACHE_PATH, encoded)
                target.chmod(0o600)
                return {
                    "models": models,
                    "source": "openrouter",
                    "fetched_at": fetched_at,
                    "stale": False,
                }
            except (
                httpx.HTTPError,
                ValueError,
                TypeError,
                UnicodeError,
                RecursionError,
            ):
                if cached is not None:
                    return {
                        **cached,
                        "stale": True,
                        "error": "OpenRouter catalog unavailable; showing cached metadata.",
                    }
                return {
                    "models": [],
                    "source": "openrouter",
                    "fetched_at": None,
                    "stale": True,
                    "error": "OpenRouter catalog unavailable or malformed; no models were inferred.",
                }

    def cached_model(self, model_id):
        """Read one cached model without any provider request, including on misses."""
        if not isinstance(model_id, str) or not 1 <= len(model_id) <= 512:
            raise ValueError("Invalid model ID")
        with self._lock:
            cached = self._read_cache(self._cache_path())
            if cached is None:
                return None
            item = next(
                (row for row in cached["models"] if row["id"] == model_id), None
            )
            if item is None:
                return None
            return {
                **item,
                **{key: cached[key] for key in ("source", "fetched_at", "stale")},
            }

    def lookup_model(self, model_id, *, allow_network=False):
        if type(allow_network) is not bool:
            raise ValueError("allow_network must be a boolean")
        if not allow_network:
            return self.cached_model(model_id)
        catalog = self.openrouter_models()
        item = next((row for row in catalog["models"] if row["id"] == model_id), None)
        return (
            {**item, **{key: catalog[key] for key in ("source", "fetched_at", "stale")}}
            if item
            else None
        )

    def openrouter_usage(self, config):
        if not is_openrouter(config):
            raise ValueError("Usage requires the official OpenRouter HTTPS endpoint")
        try:
            token = _token(config)
        except ValueError:
            token = None
        if token is None:
            return {
                "provider": "openrouter",
                "error": "Configure an OpenRouter API key to read account quota.",
                "fetched_at": None,
            }
        result = {
            "provider": "openrouter",
            "fetched_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
        }
        try:
            with self._client() as client:
                payload = self._json(client, KEY_URL, token=token, maximum=128000)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise TypeError("Malformed quota metadata")
            for key in ("limit_remaining", "usage_daily"):
                if key in data and data[key] is None:
                    result[key] = None
                elif (
                    key in data
                    and _money(data[key], allow_negative=key == "limit_remaining")
                    is not None
                ):
                    result[key] = _money(
                        data[key], allow_negative=key == "limit_remaining"
                    )
            if type(data.get("is_free_tier")) is bool:
                result["is_free_tier"] = data["is_free_tier"]
            free = data.get("free_model_daily_requests")
            if isinstance(free, dict) and all(
                type(free.get(key)) is int and 0 <= free[key] <= 1_000_000_000
                for key in ("used", "limit", "remaining")
            ):
                result["free_model_daily_requests"] = {
                    key: free[key] for key in ("used", "limit", "remaining")
                }
                for key in ("resets_at", "reset_at"):
                    value = free.get(key)
                    if isinstance(value, str) and len(value) <= 64:
                        try:
                            stamp = datetime.fromisoformat(value)
                            if stamp.tzinfo is not None:
                                result["free_model_daily_requests"][key] = value
                        except ValueError:
                            pass
            return result
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError, RecursionError):
            return {
                **result,
                "error": "OpenRouter quota unavailable; remaining request allowance is unknown.",
            }

    def probe_local(self, config):
        value = _get(config, "base_url", "")
        if not isinstance(value, str) or any(ord(char) < 33 for char in value):
            raise ValueError("Choose one explicit loopback endpoint")
        parsed = urlsplit(value)
        try:
            loopback = (
                parsed.hostname == "localhost"
                or ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
            port = parsed.port
        except ValueError:
            loopback, port = False, None
        if (
            not loopback
            or parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") not in {"", "/v1", "/api/v1"}
        ):
            raise ValueError(
                "Local discovery requires an explicit loopback server base URL"
            )
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid local endpoint port")
        base = httpx.URL(value)
        # localhost is pinned to a literal loopback address, preventing DNS
        # aliases or resolver configuration from targeting any other network.
        extensions = {}
        if parsed.hostname == "localhost":
            base = base.copy_with(host="127.0.0.1")
            extensions = {"sni_hostname": "localhost"}
        origin = base.copy_with(path="/", query=None, fragment=None)
        models_url = (
            base.copy_with(path=parsed.path.rstrip("/") + "/models")
            if parsed.path.rstrip("/")
            else base.copy_with(path="/v1/models")
        )
        result = {
            "models": [],
            "source": "local",
            "fetched_at": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            "stale": False,
        }
        try:
            token = _token(config)
            metadata_options = {
                "token": token,
                "extensions": extensions,
                "host": httpx.URL(value).netloc.decode("ascii"),
            }
            with self._client() as client:
                payload = self._json(
                    client, str(models_url), maximum=4_000_000, **metadata_options
                )
                rows = payload.get("data")
                if not isinstance(rows, list) or len(rows) > MAX_MODELS:
                    raise ValueError("Invalid local model metadata")
                props = {}
                try:
                    props = self._json(
                        client,
                        str(origin.copy_with(path="/props")),
                        maximum=128000,
                        **metadata_options,
                    )
                except (
                    httpx.HTTPError,
                    ValueError,
                    TypeError,
                    UnicodeError,
                    RecursionError,
                ):
                    pass
            settings = props.get("default_generation_settings")
            settings = settings if isinstance(settings, dict) else {}
            live_context = _integer(settings.get("n_ctx"))
            normalized = []
            for row in rows:
                item = _normalize(row, provider="local")
                meta = row.get("meta")
                meta = meta if isinstance(meta, dict) else {}
                runtime_context = (
                    live_context
                    if len(rows) == 1 or row["id"] == _get(config, "model_id")
                    else None
                )
                training_context = _integer(meta.get("n_ctx_train"))
                item["context_source"] = (
                    "runtime"
                    if runtime_context
                    else "advertised"
                    if item["context_length"]
                    else "training"
                    if training_context
                    else "unknown"
                )
                item["context_length"] = (
                    runtime_context or item["context_length"] or training_context
                )
                normalized.append(item)
            if len({item["id"] for item in normalized}) != len(normalized):
                raise ValueError("Duplicate local model IDs")
            result["models"] = normalized
            return result
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError, RecursionError):
            return {
                **result,
                "error": "Local model metadata is unavailable; no ports or alternate endpoints were scanned.",
            }

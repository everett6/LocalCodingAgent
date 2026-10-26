"""Conservative OpenRouter free-request admission, without paid-model fallback.

Share one guard across roles and model instances in an application. Each admit
reserves one attempted inference request; there is deliberately no refund for a
failed request. This is request accounting, not token or dollar accounting.

Official contract: https://openrouter.ai/docs/api_reference/limits
Only explicit server counters establish a daily allowance. Missing metadata is
unknown, and ``is_free_tier`` or dollar usage never implies a request ceiling.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import threading
import time
from typing import Any, Callable
import uuid

from local_coder.integrations.mcp import redact_data
from local_coder.providers.catalog import is_openrouter

_DAILY_SOURCES = {
    "openrouter_free_tier_daily",
    "openrouter_free_models_daily",
    "openrouter_free_model_daily_requests",
    "free_model_daily_requests",
}
_DAILY_MESSAGE = re.compile(r"\bfree[-_ ]models?[-_ ]per[-_ ]day(?:[-_ ]high[-_ ]balance)?\b", re.I)
_MODEL_ID = re.compile(r"[A-Za-z0-9~][A-Za-z0-9._:/@+ -]{0,511}\Z")


def _get(config: Any, key: str, default: Any = None) -> Any:
    return config.get(key, default) if isinstance(config, dict) else getattr(config, key, default)


def _model_id(config: Any) -> str:
    value = _get(config, "model_id", "")
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value) or redact_data(value) != value:
        return "configured-model"
    return value


def _counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    if any(type(value.get(key)) is not int or not 0 <= value[key] <= 1_000_000_000 for key in ("used", "limit", "remaining")):
        return None
    used, limit, remaining = value["used"], value["limit"], value["remaining"]
    if remaining > limit or (used < limit and used + remaining != limit) or (used >= limit and remaining != 0):
        return None
    return {"used": used, "limit": limit, "remaining": remaining}


def _reset(value: Any) -> float | None:
    """Parse a supplied reset; never manufacture one from Retry-After or time."""
    try:
        if type(value) in (int, float) or (isinstance(value, str) and re.fullmatch(r"\d{1,16}(?:\.\d+)?", value)):
            stamp = float(value)
            if stamp > 100_000_000_000:  # OpenRouter may report epoch milliseconds.
                stamp /= 1000
        elif isinstance(value, str) and len(value) <= 80:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            stamp = parsed.timestamp()
        else:
            return None
        if not math.isfinite(stamp) or not 946684800 <= stamp <= 4102444800:
            return None
        return stamp
    except (ValueError, OverflowError, OSError):
        return None


class ProviderQuotaExhausted(RuntimeError):
    """Terminal for automatic retries; a human must choose the next action.

    ``data``/``notice`` are safe structured UI/event payloads. A boolean approval
    cannot make the free endpoint usable or authorize selection of a paid model.
    """

    retryable = False

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.notice = data
        super().__init__("OpenRouter daily free-request quota exhausted. Wait for reset or explicitly select a paid model with a spending budget before resuming.")


class FreeQuotaGuard:
    """Thread-safe, synchronous admission against trusted provider metadata.

    Pass the already configured CatalogService and optionally a synchronous
    notice callback. Callbacks receive sanitized data only; their return values
    cannot grant admission. Async callers may use ``asyncio.to_thread``.

    One guard conservatively shares a free bucket across configured OpenRouter
    models/keys; it does not infer account identity from credentials. Credentials
    are passed only to the service for its existing metadata GET and are never
    retained in guard state, reservations, notices or exceptions.
    """

    def __init__(
        self, catalog_service: Any, *, notice_callback: Callable[[dict], Any] | None = None,
        clock: Callable[[], float] = time.time, ttl_seconds: float = 30,
    ):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not 0 < ttl_seconds <= 30:
            raise ValueError("Quota cache TTL must be positive and at most 30 seconds")
        self.catalog_service = catalog_service
        self.notice_callback = notice_callback
        self.clock = clock
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._day = ""
        self._last_fetch = -math.inf
        self._catalog_fetch = -math.inf
        self._verified_free: set[str] = set()
        self._remaining: int | None = None
        self._server_counts: dict[str, int] | None = None
        self._attempted = 0
        self._blocked = False
        self._unavailable = False
        self._reset_at: float | None = None
        self._last_reset_processed = -math.inf
        self._notice_id = uuid.uuid4().hex
        self._notice_sent = False

    def _time(self) -> float:
        value = float(self.clock())
        if not math.isfinite(value):
            raise ValueError("Invalid quota clock")
        return value

    def _rollover(self, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        reset_due = self._reset_at is not None and self._last_reset_processed < self._reset_at <= now
        if self._day == day and not reset_due:
            return
        self._day = day
        if reset_due:
            self._last_reset_processed = self._reset_at
        self._remaining = None
        self._server_counts = None
        self._attempted = 0
        self._last_fetch = -math.inf
        self._reset_at = None
        self._notice_id = uuid.uuid4().hex
        self._notice_sent = False
        # A previously exhausted bucket stays blocked until a fresh server
        # counter confirms capacity. Clock rollover alone never invents credits.

    def _is_free(self, config: Any, now: float) -> bool:
        if not is_openrouter(config):
            return False
        identity = _model_id(config)
        if identity.endswith(":free"):
            return True
        cached_model = getattr(self.catalog_service, "cached_model", None)
        if callable(cached_model):
            try:
                row = cached_model(identity)
                return isinstance(row, dict) and row.get("is_free") is True and row.get("pricing_complete") is True
            except Exception:
                return False
        if now < self._catalog_fetch or now - self._catalog_fetch >= 30:
            self._catalog_fetch = now
            try:
                catalog = self.catalog_service.openrouter_models()
                rows = catalog.get("models", []) if isinstance(catalog, dict) else []
                if isinstance(rows, list) and len(rows) <= 50000:
                    self._verified_free = {
                        row["id"] for row in rows if isinstance(row, dict)
                        and row.get("is_free") is True and row.get("pricing_complete") is True
                        and isinstance(row.get("id"), str) and _MODEL_ID.fullmatch(row["id"])
                        and redact_data(row["id"]) == row["id"]
                    }
            except Exception:
                # Never surface provider or credential-bearing exception text.
                pass
        return identity in self._verified_free

    def _refresh(self, config: Any, now: float, *, force: bool = False) -> None:
        if not force and 0 <= now - self._last_fetch < self.ttl_seconds:
            return
        self._last_fetch = now
        try:
            response = self.catalog_service.openrouter_usage(config)
        except Exception:
            response = None
        free = response.get("free_model_daily_requests") if isinstance(response, dict) else None
        counts = _counts(free)
        if counts is None:
            self._unavailable = True
            return
        self._unavailable = False
        if self._remaining is None:
            # Earlier attempts made while metadata was unavailable may not yet
            # appear in the server snapshot; retain them conservatively.
            self._remaining = max(0, counts["remaining"] - self._attempted)
            self._blocked = self._remaining == 0
        else:
            # Stale metadata must not restore reservations already spent locally.
            self._remaining = min(self._remaining, counts["remaining"])
            self._blocked = self._blocked or self._remaining == 0
        self._server_counts = counts
        supplied_reset = _reset(free.get("resets_at", free.get("reset_at")))
        if supplied_reset is not None and supplied_reset > self._last_reset_processed:
            self._reset_at = supplied_reset

    def _snapshot(self) -> dict[str, Any]:
        daily = None
        if self._server_counts is not None and self._remaining is not None:
            daily = {
                "used": max(self._server_counts["used"], self._server_counts["limit"] - self._remaining),
                "limit": self._server_counts["limit"], "remaining": self._remaining,
            }
        return {
            "provider": "openrouter", "unit": "requests", "utc_day": self._day or None,
            "remaining": self._remaining, "free_model_daily_requests": daily,
            "server_daily_requests": dict(self._server_counts) if self._server_counts else None,
            "attempted_requests": self._attempted, "exhausted": self._blocked or self._remaining == 0,
            "quota_known": self._remaining is not None,
            "count_source": "server_with_local_reservations" if self._server_counts else "unknown",
            "reset_at": datetime.fromtimestamp(self._reset_at, timezone.utc).isoformat() if self._reset_at is not None else None,
            "reset_source": "server" if self._reset_at is not None else None,
            "metadata_unavailable": self._unavailable,
            "cache_ttl_seconds": self.ttl_seconds,
        }

    def _exhausted(self, config: Any) -> ProviderQuotaExhausted:
        data = {
            **self._snapshot(), "type": "provider_quota_exhausted",
            "code": "free_daily_requests_exhausted", "notice_id": self._notice_id,
            "model_id": _model_id(config), "remaining": 0,
            "requires_human_action": True, "retryable": False,
            "message": "Daily free-request quota exhausted. Wait for provider reset, or explicitly choose a paid model and spending budget before resuming.",
            "choices": [
                {"id": "wait_for_reset", "label": "Wait for quota reset"},
                {"id": "choose_paid_model", "label": "Choose a paid model and budget", "requires_model_selection": True, "requires_spending_budget": True},
                {"id": "stop", "label": "Stop this job"},
            ],
            "automatic_paid_fallback": False,
        }
        if self.notice_callback is not None and not self._notice_sent:
            self._notice_sent = True
            try:
                # The callback is notification only. In particular, True does
                # not approve further free attempts or any paid selection.
                self.notice_callback(json.loads(json.dumps(data)))
            except Exception:
                pass
        return ProviderQuotaExhausted(data)

    def admit(self, config: Any) -> dict[str, Any]:
        """Reserve one attempt immediately before an actual inference HTTP call.

        Do not call this merely while planning requests. Never refund an admitted
        attempt because its inference response failed; providers count failures.
        """
        with self._lock:
            now = self._time()
            if not self._is_free(config, now):
                return {"admitted": True, "free": False, "reservation_id": None, "unit": "requests"}
            self._rollover(now)
            self._refresh(config, now)
            if self._blocked or self._remaining == 0:
                raise self._exhausted(config)
            self._attempted += 1
            if self._remaining is not None:
                self._remaining -= 1
            return {
                "admitted": True, "free": True, "reservation_id": uuid.uuid4().hex,
                "model_id": _model_id(config), "remaining": self._remaining,
                "attempted_requests": self._attempted, "unit": "requests",
                "quota_known": self._remaining is not None,
            }

    def status(self, config: Any = None, *, refresh: bool = False) -> dict[str, Any]:
        """Return sanitized local accounting; optionally refresh a free config."""
        with self._lock:
            now = self._time()
            self._rollover(now)
            if config is not None:
                if not self._is_free(config, now):
                    return {"provider": "openrouter" if is_openrouter(config) else "other", "applies": False, "unit": "requests"}
                self._refresh(config, now, force=refresh)
            return {**self._snapshot(), "applies": True}

    def observe_http_error(self, config: Any, status_code: int, body: Any, headers: Any = None) -> ProviderQuotaExhausted | None:
        """Classify only confirmed daily free limits, not RPM/upstream 429s.

        This observes an already admitted call and must not reserve it again.
        Streaming errors can pass their embedded error code/body here as well.
        Provider error text and arbitrary response headers are never returned.
        """
        if status_code != 429:
            return None
        with self._lock:
            now = self._time()
            if not self._is_free(config, now):
                return None
            self._rollover(now)
            if isinstance(body, (str, bytes)):
                if len(body) > 128000:
                    return None
                try:
                    body = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    body = {"error": {"message": body if isinstance(body, str) else ""}}
            error = body.get("error", {}) if isinstance(body, dict) else {}
            if not isinstance(error, dict):
                return None
            metadata = error.get("metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            message = error.get("message", "")
            source = metadata.get("limit_source")
            daily = source in _DAILY_SOURCES if isinstance(source, str) else False
            daily = daily or (isinstance(message, str) and len(message) <= 16000 and _DAILY_MESSAGE.search(message) is not None)
            if not daily:
                return None
            self._remaining = 0
            self._blocked = True
            nested_headers = metadata.get("headers", {})
            candidates = [metadata.get("resets_at"), metadata.get("reset_at")]
            for source_headers in (headers, nested_headers):
                if hasattr(source_headers, "items"):
                    candidates.extend(value for key, value in source_headers.items() if isinstance(key, str) and key.lower() == "x-ratelimit-reset")
            for candidate in candidates:
                stamp = _reset(candidate)
                if stamp is not None and stamp > self._last_reset_processed:
                    self._reset_at = stamp
                    break
            return self._exhausted(config)

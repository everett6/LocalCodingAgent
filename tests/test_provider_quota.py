"""Admission counts requests, including failed attempts, without paid fallback."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from local_coder.providers.quota import FreeQuotaGuard, ProviderQuotaExhausted


def config(model="example/model:free", url="https://openrouter.ai/api/v1"):
    return {"model_id": model, "base_url": url, "api_key": "never-log-this-key"}


class Clock:
    value = 1793059200.0
    def __call__(self):
        return self.value


class Catalog:
    def __init__(self, remaining=2):
        self.remaining = remaining
        self.calls = 0
        self.rows = []
    def openrouter_usage(self, selected):
        self.calls += 1
        if self.remaining is None:
            return {"error": "API key never-log-this-key unavailable"}
        return {"available": True, "free_model_daily_requests": {"used": 10 - self.remaining, "limit": 10, "remaining": self.remaining}, "usage_daily": 999.0, "is_free_tier": False}
    def openrouter_models(self, refresh=False):
        return {"models": self.rows, "stale": False, "source": "openrouter"}


def test_reserves_requests_atomically_and_charges_failed_attempts():
    service = Catalog(2)
    notices = []
    guard = FreeQuotaGuard(service, notice_callback=notices.append)
    first = guard.admit(config())
    assert first["remaining"] == 1 and first["unit"] == "requests"
    # No successful-result callback is necessary; failure also consumes allowance.
    second = guard.admit(config("another/model:free"))
    assert second["remaining"] == 0
    with pytest.raises(ProviderQuotaExhausted) as caught:
        guard.admit(config())
    assert caught.value.retryable is False
    assert caught.value.data["requires_human_action"] is True
    assert caught.value.data["remaining"] == 0
    assert len(notices) == 1
    assert {choice["id"] for choice in notices[0]["choices"]} == {"wait_for_reset", "choose_paid_model", "stop"}
    assert service.calls == 1
    assert "never-log-this-key" not in json.dumps(guard.status())
    assert "never-log-this-key" not in str(caught.value)


def test_concurrent_roles_cannot_overspend_last_requests():
    service = Catalog(3)
    guard = FreeQuotaGuard(service)
    def attempt(number):
        try:
            return guard.admit(config(f"model-{number}:free"))["admitted"]
        except ProviderQuotaExhausted:
            return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        admitted = list(pool.map(attempt, range(30)))
    assert admitted.count(True) == 3
    assert guard.status()["attempted_requests"] == 3
    assert service.calls == 1


def test_stale_refreshed_counter_never_increases_local_allowance():
    clock = Clock()
    service = Catalog(3)
    guard = FreeQuotaGuard(service, clock=clock)
    guard.admit(config())
    clock.value += 31
    guard.admit(config())
    assert guard.status()["remaining"] == 1
    clock.value += 31
    guard.admit(config())
    with pytest.raises(ProviderQuotaExhausted):
        guard.admit(config())
    assert service.calls == 3


def test_unknown_counter_never_invents_a_tier_cap_or_token_counter():
    service = Catalog(None)
    guard = FreeQuotaGuard(service)
    for _ in range(51):
        result = guard.admit(config())
        assert result["remaining"] is None
    status = guard.status()
    assert status["free_model_daily_requests"] is None
    assert status["attempted_requests"] == 51
    assert status["remaining"] is None
    assert "never-log-this-key" not in json.dumps(status)
    assert service.calls == 1


@pytest.mark.parametrize("url", ["https://openrouter.ai.evil.test/api/v1", "http://openrouter.ai/api/v1", "https://user:pass@openrouter.ai/api/v1", "https://openrouter.ai/api/v1?target=evil", "https://openrouter.ai/api/v1/elsewhere", "http://localhost:8080/v1"])
def test_only_official_config_is_subject_to_guard(url):
    service = Catalog(0)
    result = FreeQuotaGuard(service).admit(config(url=url))
    assert result["free"] is False and result["admitted"] is True
    assert service.calls == 0


def test_paid_model_does_not_probe_quota_or_inherit_exhausted_free_bucket():
    service = Catalog(0)
    guard = FreeQuotaGuard(service)
    with pytest.raises(ProviderQuotaExhausted):
        guard.admit(config())
    assert guard.admit(config("explicitly-selected/paid"))["admitted"]
    assert service.calls == 1


def test_free_router_requires_verified_catalog_pricing_not_config_flag():
    service = Catalog(0)
    guard = FreeQuotaGuard(service)
    selected = {**config("not-free"), "is_free": True}
    assert guard.admit(selected)["free"] is False
    assert service.calls == 0
    service.rows = [{"id": "openrouter/free", "is_free": True, "pricing_complete": True}]
    # Separate guard observes the newly supplied verified catalog.
    guard = FreeQuotaGuard(service)
    with pytest.raises(ProviderQuotaExhausted):
        guard.admit(config("openrouter/free"))


def test_daily_429_is_distinct_from_provider_rpm_and_cloudflare_429():
    service = Catalog(None)
    guard = FreeQuotaGuard(service)
    for body in ({"error": {"message": "Provider rate limit", "metadata": {"provider_code": 429}}}, {"error": {"message": "Rate limit exceeded"}}, "Cloudflare request limited"):
        assert guard.observe_http_error(config(), 429, body, {"X-RateLimit-Remaining": "0"}) is None
    error = guard.observe_http_error(config(), 429, {"error": {"message": "Rate limit exceeded: free-models-per-day", "metadata": {"limit_source": "openrouter_free_tier_daily"}}}, {})
    assert isinstance(error, ProviderQuotaExhausted)
    assert error.data["reset_at"] is None
    with pytest.raises(ProviderQuotaExhausted):
        guard.admit(config())


def test_only_daily_error_parses_server_reset_not_rpm_retry_after():
    guard = FreeQuotaGuard(Catalog(None))
    error = guard.observe_http_error(config(), 429, {"error": {"message": "Rate limit exceeded: free-models-per-day-high-balance"}}, {"X-RateLimit-Reset": "1793145600000", "Retry-After": "60"})
    assert error.data["reset_at"] == "2026-10-28T00:00:00+00:00"
    assert error.data["reset_source"] == "server"
    other = FreeQuotaGuard(Catalog(None)).observe_http_error(config(), 429, {"error": {"message": "Rate limit exceeded: free-models-per-day"}}, {"Retry-After": "60"})
    assert other.data["reset_at"] is None


def test_next_utc_day_requires_fresh_provider_counter_before_admission():
    clock = Clock()
    service = Catalog(0)
    guard = FreeQuotaGuard(service, clock=clock)
    with pytest.raises(ProviderQuotaExhausted):
        guard.admit(config())
    clock.value += 86400
    service.remaining = None
    with pytest.raises(ProviderQuotaExhausted):
        guard.admit(config())
    service.remaining = 2
    clock.value += 31
    assert guard.admit(config())["remaining"] == 1


def test_human_notice_callback_cannot_approve_or_trigger_paid_fallback():
    selected = config()
    guard = FreeQuotaGuard(Catalog(0), notice_callback=lambda notice: True)
    with pytest.raises(ProviderQuotaExhausted):
        guard.admit(selected)
    assert selected["model_id"].endswith(":free")
    def broken_callback(notice):
        raise RuntimeError("never-log-this-key")
    guard = FreeQuotaGuard(Catalog(0), notice_callback=broken_callback)
    with pytest.raises(ProviderQuotaExhausted) as caught:
        guard.admit(selected)
    assert "never-log-this-key" not in str(caught.value)


def test_usage_fetch_failure_is_sanitized_and_cached():
    class Broken(Catalog):
        def openrouter_usage(self, selected):
            self.calls += 1
            raise RuntimeError(selected["api_key"])
    service = Broken()
    guard = FreeQuotaGuard(service)
    assert guard.admit(config())["remaining"] is None
    guard.admit(config())
    assert service.calls == 1
    assert "never-log-this-key" not in json.dumps(guard.status())

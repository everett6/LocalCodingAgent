import json
import os
import stat
from types import SimpleNamespace

import httpx
import pytest

from local_coder.providers.catalog import CatalogService, is_openrouter


def model(identity="nvidia/synthetic-nemotron:free", **changes):
    return {
        "id": identity,
        "name": "Synthetic test model",
        "context_length": 262144,
        "top_provider": {"max_completion_tokens": 65536},
        "pricing": {"prompt": "0", "completion": "0"},
        "supported_parameters": ["tools", "max_tokens"],
        **changes,
    }


def config(url="https://openrouter.ai/api/v1", key="synthetic-private-key"):
    return SimpleNamespace(
        base_url=url,
        backend="openai_compatible",
        model_id="test",
        resolve_api_key=lambda: key,
    )


def test_full_public_catalog_normalizes_prices_caps_tools_without_auth(tmp_path):
    requests = []
    rows = [
        model(),
        model(
            "vendor/paid",
            pricing={
                "prompt": "0.0000015",
                "completion": "0.000007",
                "request": "0.001",
            },
        ),
        model(
            "vendor/request-fee:free",
            pricing={"prompt": "0", "completion": "0", "request": "0.001"},
        ),
        model("vendor/unknown", pricing={"prompt": "0"}),
        model(
            "vendor/unknown-extra",
            pricing={"prompt": "0", "completion": "0", "image": None},
        ),
    ]

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": rows, "total_count": len(rows)})

    service = CatalogService(tmp_path, transport=httpx.MockTransport(respond))
    result = service.openrouter_models()
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/models"
    assert "authorization" not in requests[0].headers
    free, paid, fee, unknown, extra = result["models"]
    assert free["is_free"] and free["free_variant"] and free["supports_tools"]
    assert free["context_length"] == 262144 and free["max_output_tokens"] == 65536
    assert (
        paid["input_cost_per_million"] == 1.5 and paid["output_cost_per_million"] == 7
    )
    assert paid["request_cost_usd"] == 0.001
    assert not fee["is_free"] and not unknown["is_free"] and not extra["is_free"]
    assert result["source"] == "openrouter" and not result["stale"]
    cache = tmp_path / ".local-coder/providers/openrouter-models.json"
    if os.name != 'nt':
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    else:
        assert cache.stat().st_mode & stat.S_IWRITE
    assert "synthetic-private-key" not in cache.read_text()


def test_catalog_has_no_silent_model_truncation(tmp_path):
    rows = [model(f"test/model-{index}") for index in range(10001)]
    service = CatalogService(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": rows})
        ),
    )
    result = service.openrouter_models()
    assert len(result["models"]) == 10001
    assert result["models"][-1]["id"] == "test/model-10000"


def test_cache_ttl_and_explicit_stale_offline_metadata(tmp_path):
    now = [1000.0]
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) > 1:
            raise httpx.ConnectError(
                "Do not expose request headers or arbitrary error details"
            )
        return httpx.Response(200, json={"data": [model()]})

    service = CatalogService(
        tmp_path, transport=httpx.MockTransport(respond), clock=lambda: now[0]
    )
    first = service.openrouter_models()
    assert service.openrouter_models()["source"] == "cache" and len(calls) == 1
    now[0] += 901
    stale = service.openrouter_models()
    assert stale["stale"] is True and stale["fetched_at"] == first["fetched_at"]
    assert stale["models"] == first["models"] and "error" in stale
    assert "request headers" not in stale["error"]


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"name": "missing ID"}]},
        {"data": [model(), model()]},
        {"data": [model()], "total_count": 3},
        {"data": "bad"},
    ],
)
def test_malformed_or_incomplete_catalog_fails_without_inventing_models(
    tmp_path, payload
):
    service = CatalogService(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )
    result = service.openrouter_models()
    assert result["models"] == [] and result["error"]


def test_usage_reports_request_counters_and_only_safe_quota_fields(tmp_path):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "free_model_daily_requests": {
                        "used": 12,
                        "limit": 50,
                        "remaining": 38,
                    },
                    "limit_remaining": 1.25,
                    "usage_daily": 0.5,
                    "is_free_tier": True,
                    "label": "synthetic-private-key",
                    "secret": "must not return",
                }
            },
        )

    service = CatalogService(tmp_path, transport=httpx.MockTransport(respond))
    result = service.openrouter_usage(config())
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/key"
    assert seen[0].headers["authorization"] == "Bearer synthetic-private-key"
    assert result["free_model_daily_requests"] == {
        "used": 12,
        "limit": 50,
        "remaining": 38,
    }
    assert result["limit_remaining"] == 1.25 and result["is_free_tier"] is True
    assert "synthetic-private-key" not in json.dumps(result) and "secret" not in result
    assert not list((tmp_path / ".local-coder/providers").glob("*usage*"))


def test_missing_account_scoped_quota_stays_unknown(tmp_path):
    service = CatalogService(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": {"limit_remaining": None, "is_free_tier": False}}
            )
        ),
    )
    result = service.openrouter_usage(config())
    assert "free_model_daily_requests" not in result
    assert result["limit_remaining"] is None


@pytest.mark.parametrize(
    "url",
    [
        "https://openrouter.ai.evil.test/api/v1",
        "https://user:pass@openrouter.ai/api/v1",
        "https://openrouter.ai:444/api/v1",
        "http://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?key=secret",
        "https://openrouter.ai/arbitrary",
        "http://127.0.0.1:8000/v1",
    ],
)
def test_openrouter_usage_rejects_auth_url_confusion_before_request(tmp_path, url):
    calls = []
    service = CatalogService(
        tmp_path, transport=httpx.MockTransport(lambda request: calls.append(request))
    )
    assert not is_openrouter(config(url))
    with pytest.raises(ValueError):
        service.openrouter_usage(config(url))
    assert calls == []


def test_usage_and_catalog_never_follow_redirects_or_echo_body_errors(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://evil.test/steal"},
            text="synthetic-private-key",
        )

    service = CatalogService(tmp_path, transport=httpx.MockTransport(respond))
    assert service.openrouter_models()["error"]
    usage = service.openrouter_usage(config())
    assert usage["error"] and "synthetic-private-key" not in json.dumps(usage)
    assert len(calls) == 2 and all(
        request.url.host == "openrouter.ai" for request in calls
    )


def test_local_probe_reads_only_explicit_endpoint_metadata_and_props(tmp_path):
    seen = []

    def respond(request):
        seen.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 9876
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "loaded-local-model",
                            "meta": {"n_ctx_train": 131072},
                            "secret": "must not return",
                        }
                    ]
                },
            )
        assert request.url.path == "/props"
        return httpx.Response(
            200,
            json={
                "default_generation_settings": {
                    "n_ctx": 32768,
                    "params": {"n_predict": -1},
                },
                "model_path": "/private/model.gguf",
            },
        )

    service = CatalogService(tmp_path, transport=httpx.MockTransport(respond))
    result = service.probe_local(config("http://127.0.0.1:9876/v1", key=None))
    assert len(seen) == 2 and all(request.method == "GET" for request in seen)
    assert result["models"][0]["id"] == "loaded-local-model"
    assert result["models"][0]["context_length"] == 32768
    assert result["models"][0]["context_source"] == "runtime"
    assert result["models"][0]["provider"] == "local"
    assert result["models"][0]["max_output_tokens"] is None
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.2:8000/v1",
        "https://example.com/v1",
        "http://localhost.evil/v1",
        "http://127.0.0.1:8000/private",
        "http://user:pass@localhost:8000/v1",
    ],
)
def test_local_probe_cannot_scan_other_hosts_or_arbitrary_paths(tmp_path, url):
    service = CatalogService(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("No request expected")
        ),
    )
    with pytest.raises(ValueError):
        service.probe_local(config(url))


def test_provider_cache_rejects_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".local-coder").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        CatalogService(tmp_path).openrouter_models()


def test_official_aliases_and_cached_unknown_metadata_are_preserved(tmp_path):
    rows = [
        model("~vendor/latest"),
        model("vendor/unknown", top_provider=None, pricing=None),
    ]
    service = CatalogService(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": rows})
        ),
    )
    first = service.openrouter_models()
    assert [item["id"] for item in first["models"]] == [
        "~vendor/latest",
        "vendor/unknown",
    ]
    assert service.openrouter_models()["models"] == first["models"]


def test_public_catalog_ignores_injected_client_credentials(tmp_path):
    def respond(request):
        assert (
            "authorization" not in request.headers and "cookie" not in request.headers
        )
        return httpx.Response(200, json={"data": [model()]})

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("private", "secret"),
        headers={
            "Authorization": "Bearer do-not-inherit",
            "Cookie": "session=do-not-inherit",
        },
        follow_redirects=True,
    ) as client:
        assert CatalogService(tmp_path, client=client).openrouter_models()["models"]


def test_invalid_cache_shape_is_ignored_and_refetched(tmp_path):
    directory = tmp_path / ".local-coder/providers"
    directory.mkdir(parents=True)
    (directory / "openrouter-models.json").write_text("[]")
    service = CatalogService(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [model()]})
        ),
    )
    assert service.openrouter_models()["models"]


def test_cached_model_lookup_never_connects_and_retains_stale_provenance(tmp_path):
    now = [1000.0]
    service = CatalogService(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [model()]})
        ),
        clock=lambda: now[0],
    )
    assert service.cached_model("unknown") is None
    service.openrouter_models()
    now[0] += 1000
    service.transport = httpx.MockTransport(
        lambda request: pytest.fail("Cache lookup must never contact a provider")
    )
    item = service.cached_model("nvidia/synthetic-nemotron:free")
    assert item["stale"] is True and item["source"] == "cache"
    assert service.lookup_model("nvidia/synthetic-nemotron:free")["id"] == item["id"]


def test_usage_preserves_negative_credit_balance_and_only_verified_reset_metadata(
    tmp_path,
):
    payload = {
        "data": {
            "limit_remaining": -1.25,
            "usage_daily": 2,
            "free_model_daily_requests": {
                "used": 12,
                "limit": 50,
                "remaining": 38,
                "resets_at": "2026-10-27T00:00:00Z",
                "reset_at": "Bearer must-not-return-this",
            },
        }
    }
    service = CatalogService(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )
    result = service.openrouter_usage(config())
    assert result["limit_remaining"] == -1.25
    assert result["free_model_daily_requests"]["resets_at"] == "2026-10-27T00:00:00Z"
    assert "reset_at" not in result["free_model_daily_requests"]


def test_local_path_model_id_and_authenticated_metadata_keep_exact_identity(tmp_path):
    def respond(request):
        assert request.headers["Authorization"] == "Bearer local-explicit-key"
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "../models/local model.gguf"}]}
            )
        return httpx.Response(404)

    result = CatalogService(
        tmp_path, transport=httpx.MockTransport(respond)
    ).probe_local(config("http://127.0.0.1:9876/v1", key="local-explicit-key"))
    assert result["models"][0]["id"] == "../models/local model.gguf"
    assert result["models"][0]["context_length"] is None


def test_advertised_oversized_body_is_rejected_before_reading(tmp_path):
    response = httpx.Response(
        200, headers={"content-length": "999999999"}, json={"data": [model()]}
    )
    service = CatalogService(
        tmp_path, transport=httpx.MockTransport(lambda request: response)
    )
    assert service.openrouter_models()["error"]


def test_local_training_context_is_labeled_without_claiming_runtime_capacity(tmp_path):
    def respond(request):
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "local", "meta": {"n_ctx_train": 131072}}]}
            )
        return httpx.Response(404)

    item = CatalogService(tmp_path, transport=httpx.MockTransport(respond)).probe_local(
        config("http://127.0.0.1:9876/v1", key=None)
    )["models"][0]
    assert item["context_length"] == 131072 and item["context_source"] == "training"

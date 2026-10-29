import threading
from types import SimpleNamespace

import httpx
import pytest

from local_coder.harness_api import HarnessAPI
from local_coder.provider_api import ProviderAPI
from local_coder.providers.catalog import CatalogService
from local_coder.types import ModelConfig


class FixtureAPI(HarnessAPI, ProviderAPI):
    def __init__(self, root):
        self.project_root = str(root)
        self.config_path = None
        self.config_overrides = []
        self.model_name = None
        self._job_lock = threading.Lock()
        self._using_inference = False
        self._provider = None
        self.inference = SimpleNamespace(status=lambda: {"state": "stopped"})
        self.events = []
        self._init_harness_api()
        self._init_provider_api()

    def _event(self, event):
        self.events.append(event)

    def _update_settings(self, body):
        self._provider = ModelConfig(name="selected", **{
            key: value for key, value in body.items() if key != "name"
        })
        return {"provider": self._provider.model_dump(mode="json")}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "missing.yaml"))
    return FixtureAPI(tmp_path)


def provider(**changes):
    return {
        "backend": "openai_compatible",
        "model_id": "fixture/model",
        "base_url": "http://127.0.0.1:18080/v1",
        **changes,
    }


def test_provider_routes_are_explicit_and_public_catalog_uses_no_key(api, tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{
            "id": "nvidia/fixture:free", "name": "Fixture free model",
            "context_length": 32768,
            "pricing": {"prompt": "0", "completion": "0"},
        }]})

    api.catalog_service = CatalogService(tmp_path, transport=httpx.MockTransport(respond))
    handled, catalog = api._provider_get("/providers/models", {"refresh": ["1"]})
    assert handled and catalog["models"][0]["is_free"]
    assert len(requests) == 1 and "authorization" not in requests[0].headers
    assert api._provider_get("/unknown", {}) == (False, None)
    assert api._provider_post("/unknown", {}) == (False, None)
    for query in ({"refresh": ["true"]}, {"refresh": ["0", "1"]}, {"source": ["arbitrary"]}):
        with pytest.raises(ValueError):
            api._provider_get("/providers/models", query)


def test_profile_routes_keep_secrets_out_and_select_only_when_idle(api):
    handled, record = api._provider_post("/providers/profiles/save", {
        "name": "Local test", "provider": provider(api_key="synthetic-secret")
    })
    assert handled and "synthetic-secret" not in str(record)
    identity = record["id"]
    api._job_lock.acquire()
    try:
        with pytest.raises(ValueError, match="idle|active"):
            api._provider_post(f"/providers/profiles/{identity}/select", {})
    finally:
        api._job_lock.release()
    handled, selected = api._provider_post(f"/providers/profiles/{identity}/select", {})
    assert handled and selected["provider"]["model_id"] == "fixture/model"
    assert api._provider_get("/providers/profiles", {})[1]["active_profile"] == identity
    assert api._provider_post(f"/providers/profiles/{identity}/delete", {})[1]["deleted"]
    assert api._active_provider_profile is None


def test_local_metadata_discovery_never_reuses_a_different_endpoint_key(api, monkeypatch):
    api._provider = ModelConfig(name="local", **provider(api_key="synthetic-secret"))
    observed = []
    monkeypatch.setattr(api.catalog_service, "probe_local", lambda config: observed.append(config) or {"models": []})
    api._provider_post("/providers/discover", {"base_url": "http://127.0.0.1:18081/v1"})
    assert "api_key" not in observed[-1]
    api._provider_post("/providers/discover", {"base_url": "http://127.0.0.1:18080/v1/"})
    assert observed[-1]["api_key"] == "synthetic-secret"
    api._provider_post("/providers/discover", {"base_url": "http://127.0.0.1:18080/v1", "api_key_env": "OTHER_KEY"})
    assert "api_key" not in observed[-1]
    with pytest.raises(ValueError):
        api._provider_post("/providers/discover", {"backend": "anthropic", "base_url": "http://127.0.0.1:18080/v1"})


def test_discovery_selection_only_returns_config_and_never_starts_or_prefetches(api, monkeypatch):
    observed = []
    monkeypatch.setattr(api.runtime_discovery, "select", lambda **paths: observed.append(paths) or {"model_path": "/fixture/model.gguf", "context_size": 4096})
    result = api._provider_post("/inference/discovery/select", {"model_path": "/fixture/model.gguf"})
    assert result == (True, {"model_path": "/fixture/model.gguf", "context_size": 4096})
    assert observed == [{"model_path": "/fixture/model.gguf"}]
    api.inference = SimpleNamespace(status=lambda: {"state": "running"})
    with pytest.raises(ValueError, match="Stop inference"):
        api._provider_post("/inference/discovery/select", {"model_path": "/fixture/model.gguf"})
    assert len(observed) == 1


def test_discovery_and_defaults_mutations_require_idle_and_shapes_are_strict(api, monkeypatch):
    monkeypatch.setattr(api.runtime_discovery, "start", lambda roots=None: {"roots": roots, "state": "scanning"})
    assert api._provider_post("/inference/discover", {"roots": ["/fixture"]})[1]["roots"] == ["/fixture"]
    api._job_lock.acquire()
    try:
        for path, body in [
            ("/inference/discover", {}),
            ("/inference/discovery/select", {"runtime_path": "/fixture/llama-server"}),
            ("/engagements/defaults", {}),
        ]:
            with pytest.raises(ValueError, match="idle|active"):
                api._provider_post(path, body)
    finally:
        api._job_lock.release()
    with pytest.raises(ValueError):
        api._provider_post("/inference/discovery/select", {"shell": "anything"})


def test_usage_quota_shapes_are_sanitized_and_never_auto_switch(api, monkeypatch):
    api._provider = ModelConfig(name="local", **provider())
    assert api._provider_get("/providers/usage", {}) == (True, {"provider": "other", "applies": False})
    monkeypatch.setattr(api.free_quota_guard, "status", lambda config, refresh=False: {
        "applies": True, "exhausted": True, "remaining": 0, "unit": "requests"
    })
    result = api._provider_get("/providers/quota", {})[1]
    assert result["blocked"] and result["automatic_paid_fallback"] is False
    assert api._provider.model_id == "fixture/model"
    api._provider_quota_notice({"message": "Daily free allowance exhausted", "remaining": 0})
    assert api.events[-1].event_type == "provider_quota_exhausted"


def test_defaults_prefill_is_local_only_and_round_trips(api):
    handled, defaults = api._provider_get("/engagements/defaults", {})
    assert handled and defaults["settings"]["origins"] == []
    assert defaults["settings"]["active_http"] is False
    updated = api._provider_post("/engagements/defaults", {"settings": {"paths": ["."]}})[1]
    assert updated["settings"]["paths"] == ["."]
    assert updated["preview"]["network_requests_on_save"] == 0


def test_remote_dispatch_exposes_profiles_defaults_and_discovery_before_generic_routes(tmp_path, monkeypatch):
    from local_coder.remote import RemoteControlServer

    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "missing.yaml"))
    server = RemoteControlServer(str(tmp_path))
    assert server.handle("GET", "/providers/profiles")[1]["profiles"] == []
    status, defaults = server.handle("GET", "/engagements/defaults")
    assert status == 200 and defaults["settings"]["active_http"] is False
    assert server.handle("GET", "/inference/discovery")[1]["state"] == "idle"
    status, record = server.handle("POST", "/providers/profiles/save", {
        "name": "Local", "provider": provider(api_key="private-not-saved")
    })
    assert status == 200 and "private-not-saved" not in str(record)
    selected = server.handle("POST", f"/providers/profiles/{record['id']}/select", {})
    assert selected[0] == 200 and selected[1]["provider"]["key_set"] is False
    assert server.handle("GET", "/settings?mode=bug_hunting")[1]["context_policy"]["mode"] == "bug_hunting"
    server._job_lock.acquire()
    try:
        assert server.handle("POST", f"/providers/profiles/{record['id']}/select", {})[0] == 400
    finally:
        server._job_lock.release()


def test_remote_one_click_redteam_uses_local_engagement_without_inference_network(tmp_path, monkeypatch):
    from local_coder.remote import RemoteControlServer

    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "missing.yaml"))
    server = RemoteControlServer(str(tmp_path))
    observed = []
    completed = threading.Event()

    class FakeCoordinator:
        async def run(self, request, **options):
            observed.append(options)
            return "Local fixture review completed"

        class Manager:
            async def close_all(self):
                completed.set()

        model_manager = Manager()

    monkeypatch.setattr(server, "_coordinator", lambda: FakeCoordinator())
    status, job = server.handle("POST", "/jobs", {
        "request": "Review this project's source code", "mode": "red_team"
    })
    assert status == 202 and completed.wait(2)
    # close_all precedes durable completion; wait for the worker, not that
    # intermediate callback, before asserting the persisted terminal status.
    with server.lock:
        running = server._jobs.get(job["session_id"])
    if running is not None:
        running["thread"].join(timeout=2)
        assert not running["thread"].is_alive()
    engagement = server.engagements.load(observed[0]["engagement_id"])
    assert engagement["scope"]["origins"] == []
    assert engagement["scope"]["active_http"] is False
    assert engagement["scope"]["authorization"]
    assert server.sessions.get(job["session_id"])["status"] == "completed"

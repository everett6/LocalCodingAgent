"""Routing settings are preferences, never provider/inference actions."""
import json
import threading
from types import SimpleNamespace

import pytest

from local_coder.auto_routing_api import ECONOMY_ID, SETTINGS_PATH
from local_coder.harness_api import HarnessAPI
from local_coder.provider_api import ProviderAPI
from local_coder.types import ModelConfig, RoutingConfig


def model(identity="selected", **updates):
    return ModelConfig(name=identity, backend="openai_compatible", model_id="vendor/selected",
                       base_url="https://openrouter.ai/api/v1", api_key="secret-fixture-token",
                       context_length=64000, max_tokens=4096, **updates)


class Host(HarnessAPI, ProviderAPI):
    def __init__(self, root):
        self.project_root = str(root)
        self.config_path = None
        self.config_overrides = []
        self.model_name = None
        self._job_lock = threading.Lock()
        self._init_harness_api()
        self._using_inference = False
        self._provider = model()
        self.local = None
        self._init_provider_api()
        self.catalog = {}
        self.catalog_service = SimpleNamespace(cached_model=lambda key: self.catalog.get(key))

    def local_inference_provider(self):
        return self.local

    def config(self):
        return SimpleNamespace(routing=RoutingConfig(), models={"default": self._provider.model_copy(deep=True)})

    def _routing_settings_status(self):
        return self._apply_auto_routing(self.config(), self._provider)


def catalog_row(**updates):
    return {"pricing_complete": True, "input_cost_per_million": 1,
            "output_cost_per_million": 2, "request_cost_usd": 0,
            "is_free": False, "supports_tools": True, "context_length": 32000,
            "max_output_tokens": 1024, **updates}


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_settings_round_trip_strict_shape_idle_and_no_credentials(host, tmp_path):
    result = host._provider_post("/routing/settings", {"auto_enabled": False,
        "classifier_timeout_seconds": .5, "use_loaded_local_classifier": True})
    assert result[0] and result[1]["settings"]["auto_enabled"] is False
    saved = json.loads((tmp_path / SETTINGS_PATH).read_text())
    assert saved["use_loaded_local_classifier"] is True
    assert "secret-fixture-token" not in str(result) + json.dumps(saved)
    assert Host(tmp_path)._provider_get("/routing/settings", {})[1]["settings"]["use_loaded_local_classifier"]
    host._job_lock.acquire()
    try:
        with pytest.raises(ValueError, match="idle|active"):
            host._provider_post("/routing/settings", {"auto_enabled": True})
    finally:
        host._job_lock.release()
    for body in ({"api_key": "forbidden"}, {"auto_enabled": 1}, {"allow_cross_provider": "false"},
                 {"classifier_timeout_seconds": True}, {"classifier_timeout_seconds": float("nan")},
                 {"classifier_timeout_seconds": 6}, {"economy_model": "https://secret@host"},
                 {"local_classifier": []}, {"use_loaded_local_classifier": 0}):
        with pytest.raises(ValueError):
            host._provider_post("/routing/settings", body)
    with pytest.raises(ValueError):
        host._provider_get("/routing/settings", {"refresh": ["1"]})


def test_cached_candidate_retains_server_credentials_and_user_limits(host):
    host.catalog = {"vendor/selected": catalog_row(input_cost_per_million=3), ECONOMY_ID: catalog_row()}
    config = host.config()
    result = host._apply_auto_routing(config, host._provider)
    economy = config.models["_routing_economy"]
    assert config.routing.economy_model == economy.name
    assert economy.model_id == ECONOMY_ID and economy.profile == "openrouter_deepseek"
    assert economy.context_length == 32000 and economy.max_tokens == 1024
    assert economy.resolve_api_key() == "secret-fixture-token"
    assert host._provider.context_length == 64000
    assert result["status"]["economy_model_id"] == ECONOMY_ID
    assert "secret-fixture-token" not in str(result)


@pytest.mark.parametrize("change", ["free", "free-id", "missing", "unknown-price", "expensive", "no-tools", "no-context", "wrong-host", "stale"])
def test_no_synthesis_without_comparable_paid_official_candidate(host, change):
    host.catalog = {"vendor/selected": catalog_row(input_cost_per_million=3), ECONOMY_ID: catalog_row()}
    if change == "free": host.catalog["vendor/selected"]["is_free"] = True
    elif change == "free-id": host._provider.model_id += ":free"
    elif change == "missing": host.catalog.pop(ECONOMY_ID)
    elif change == "unknown-price": host.catalog["vendor/selected"]["pricing_complete"] = False
    elif change == "expensive": host.catalog[ECONOMY_ID]["request_cost_usd"] = 1
    elif change == "no-tools": host.catalog[ECONOMY_ID]["supports_tools"] = False
    elif change == "no-context": host.catalog[ECONOMY_ID]["context_length"] = None
    elif change == "wrong-host": host._provider.base_url = "https://openrouter.ai.evil/api/v1"
    elif change == "stale": host.catalog[ECONOMY_ID]["stale"] = True
    config = host.config()
    assert not host._apply_auto_routing(config, host._provider)["status"]["economy_available"]
    assert "_routing_economy" not in config.models


def test_yaml_defaults_explicit_economy_and_preference_override(host):
    config = host.config()
    config.routing.economy_model = "custom"
    config.models["custom"] = model("custom")
    result = host._apply_auto_routing(config, host._provider)
    assert result["settings"]["economy_model"] == "custom"
    assert result["status"]["economy_available"]
    assert "_routing_economy" not in config.models
    host._provider_post("/routing/settings", {"auto_enabled": False})
    config = host.config()
    assert not host._apply_auto_routing(config, host._provider)["settings"]["auto_enabled"]


def test_ready_local_classifier_is_opt_in_and_never_started(host):
    host.local = model("local")
    host.local.base_url = "http://127.0.0.1:8090/v1"
    assert not host._routing_settings_status()["status"]["classifier_ready"]
    result = host._provider_post("/routing/settings", {"use_loaded_local_classifier": True})[1]
    assert result["status"]["classifier_ready"]
    config = host.config()
    host._apply_auto_routing(config, host._provider)
    classifier = config.models["_routing_classifier"]
    assert classifier.max_tokens == 128 and classifier.input_cost_per_million == 0
    host.local = None
    assert not host._routing_settings_status()["status"]["classifier_ready"]


def test_settings_symlink_rejected(host, tmp_path):
    path = tmp_path / SETTINGS_PATH
    path.parent.mkdir(parents=True)
    target = tmp_path / "untouched"
    target.write_text("untouched")
    path.symlink_to(target)
    with pytest.raises(ValueError):
        host._provider_post("/routing/settings", {"auto_enabled": False})
    assert target.read_text() == "untouched"


def test_configured_classifier_is_unverified_until_matching_runtime_is_ready(host):
    config = host.config()
    config.routing.local_classifier = "classifier"
    classifier = model("classifier")
    classifier.base_url = "http://127.0.0.1:8090/v1"
    config.models["classifier"] = classifier
    status = host._apply_auto_routing(config, host._provider)["status"]
    assert status["classifier_configured"] and not status["classifier_ready"]
    assert status["classifier_state"] == "configured_unverified"
    host.local = classifier
    assert host._apply_auto_routing(config, host._provider)["status"]["classifier_state"] == "ready"


def test_equal_prices_and_non_openai_backend_do_not_create_candidate(host):
    host.catalog = {"vendor/selected": catalog_row(), ECONOMY_ID: catalog_row()}
    assert not host._routing_settings_status()["status"]["economy_available"]
    host.catalog["vendor/selected"]["input_cost_per_million"] = 3
    host._provider.backend = "anthropic"
    assert not host._routing_settings_status()["status"]["economy_available"]


@pytest.mark.parametrize("selected_url,economy_url,allow,expected", [
    ("http://127.0.0.1:8090/v1", "https://openrouter.ai/api/v1", True, False),
    ("https://openrouter.ai/api/v1", "https://other.example/v1", False, False),
    ("https://openrouter.ai/api/v1", "https://other.example/v1", True, True),
    ("https://openrouter.ai/api/v1", "http://user@127.0.0.1:8090/v1", True, False),
])
def test_configured_economy_status_obeys_runtime_boundaries(host, selected_url, economy_url, allow, expected):
    host._provider.base_url = selected_url
    config = host.config()
    config.routing.economy_model = "custom"
    config.routing.allow_cross_provider = allow
    config.models["custom"] = model("custom")
    config.models["custom"].base_url = economy_url
    status = host._apply_auto_routing(config, host._provider)["status"]
    assert status["economy_available"] is expected
    if not expected:
        assert "available" not in status["reason"].lower()


def test_economy_status_rejects_confirmed_missing_tool_support(host, monkeypatch):
    config = host.config()
    config.routing.economy_model = "custom"
    config.models["custom"] = model("custom")
    monkeypatch.setattr("local_coder.models.manager.resolve_profile",
                        lambda *args: SimpleNamespace(capabilities={"tools": False}))
    assert not host._apply_auto_routing(config, host._provider)["status"]["economy_available"]


def test_unsupported_local_classifier_backend_is_never_reported_ready(host):
    from local_coder.types import ModelBackend

    classifier = model("classifier")
    classifier.base_url = "http://127.0.0.1:8090/v1"
    classifier.backend = ModelBackend.OLLAMA
    host.local = classifier
    config = host.config()
    config.routing.local_classifier = "classifier"
    config.models["classifier"] = classifier
    status = host._apply_auto_routing(config, host._provider)["status"]
    assert status["classifier_configured"]
    assert not status["classifier_ready"]
    assert not status["classifier_local_configured"]
    assert status["classifier_state"] == "unsupported"


def test_loaded_classifier_requires_compatible_backend_and_effective_endpoint(host):
    from local_coder.types import ModelBackend

    host.local = model("local")
    host.local.backend = ModelBackend.OLLAMA
    host.local.base_url = "http://127.0.0.1:8090/v1"
    result = host._provider_post("/routing/settings", {"use_loaded_local_classifier": True})[1]
    assert not result["status"]["classifier_ready"]
    host.local.backend = ModelBackend.OPENAI_COMPAT
    config = host.config()
    config.routing.local_classifier = "classifier"
    config.models["classifier"] = host.local.model_copy(update={"name": "classifier", "backend": ModelBackend.LLAMACPP})
    host.local.backend = ModelBackend.ANTHROPIC
    assert not host._apply_auto_routing(config, host._provider)["status"]["classifier_ready"]


def test_routing_save_replace_failure_preserves_previous_preferences(host, tmp_path, monkeypatch):
    host._provider_post("/routing/settings", {"auto_enabled": False})
    path = tmp_path / SETTINGS_PATH
    previous = path.read_bytes()

    def fail_replace(*args):
        raise OSError("fixture replacement failure")

    monkeypatch.setattr("local_coder.auto_routing_api.os.replace", fail_replace)
    with pytest.raises(OSError, match="fixture replacement"):
        host._provider_post("/routing/settings", {"auto_enabled": True})
    assert path.read_bytes() == previous
    assert list(path.parent.iterdir()) == [path]


def test_routing_save_is_private_and_keeps_unrelated_preferences(host, tmp_path):
    import os
    import stat

    host._provider_post("/routing/settings", {"auto_enabled": False, "classifier_timeout_seconds": .5})
    host._provider_post("/routing/settings", {"use_loaded_local_classifier": True})
    path = tmp_path / SETTINGS_PATH
    saved = json.loads(path.read_text())
    assert saved == {"auto_enabled": False, "classifier_timeout_seconds": .5, "use_loaded_local_classifier": True}
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_routing_save_flush_failure_does_not_replace_preferences(host, tmp_path, monkeypatch):
    host._provider_post("/routing/settings", {"auto_enabled": False})
    path = tmp_path / SETTINGS_PATH
    previous = path.read_bytes()

    def fail_flush(*args):
        raise OSError("fixture flush failure")

    monkeypatch.setattr("local_coder.auto_routing_api.os.fsync", fail_flush)
    with pytest.raises(OSError, match="fixture flush"):
        host._provider_post("/routing/settings", {"auto_enabled": True})
    assert path.read_bytes() == previous
    assert list(path.parent.iterdir()) == [path]


def test_overlapping_routing_save_keeps_idle_gate(host, tmp_path, monkeypatch):
    import os

    entered, finish = threading.Event(), threading.Event()
    real_replace = os.replace
    errors = []

    def paused_replace(*args):
        entered.set()
        assert finish.wait(5)
        return real_replace(*args)

    def save():
        try:
            host._provider_post("/routing/settings", {"auto_enabled": False})
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr("local_coder.auto_routing_api.os.replace", paused_replace)
    worker = threading.Thread(target=save)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ValueError, match="idle|active"):
            host._provider_post("/routing/settings", {"auto_enabled": True})
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert json.loads((tmp_path / SETTINGS_PATH).read_text()) == {"auto_enabled": False}


def test_equivalent_classifier_endpoints_can_match_loaded_runtime(host):
    host.local = model("local")
    host.local.base_url = "http://127.0.0.1:80/v1/"
    config = host.config()
    config.routing.local_classifier = "classifier"
    config.models["classifier"] = host.local.model_copy(update={"name": "classifier", "base_url": "http://127.0.0.1/v1"})
    assert host._apply_auto_routing(config, host._provider)["status"]["classifier_ready"]

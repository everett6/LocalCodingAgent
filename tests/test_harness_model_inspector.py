"""The inspector follows configured routing without exposing provider credentials."""

from types import SimpleNamespace
import json

import pytest

from local_coder.harness_api import HarnessAPI
from local_coder.types import ModelConfig, ProjectConfig


@pytest.fixture
def inspector(tmp_path, monkeypatch):
    config = ProjectConfig(models={
        "default": ModelConfig(name="default", model_id="vendor/coder", api_key="private-value"),
        "audit": ModelConfig(name="audit", model_id="vendor/auditor"),
    })
    config.agentic.role_models["security"] = "audit"
    monkeypatch.setattr("local_coder.orchestrator.config_loader.load_config", lambda *a, **k: config)
    api = HarnessAPI()
    api.project_root = str(tmp_path)
    api._init_harness_api()
    calls = []

    def resolve(model, role, overrides):
        calls.append((model.model_id, role.value, overrides))
        return SimpleNamespace(describe=lambda: {"identity": model.model_id, "role": role.value})

    monkeypatch.setattr("local_coder.harness_api.resolve_profile", resolve, raising=False)
    return api, calls


def test_inspector_follows_role_routing_explicit_model_and_overrides(inspector):
    api, calls = inspector
    api._harness_override = api.harness_settings().model_copy(update={"profile_overrides": {"strategy": "small_steps"}})
    payload = api._harness_get("/harness", {"mode": ["cybersecurity"]})
    assert payload["model_profile"] == {"identity": "vendor/auditor", "role": "security"}
    assert calls[-1] == ("vendor/auditor", "security", {"strategy": "small_steps"})
    api.model_name = "default"
    payload = api._harness_get("/harness", {"mode": ["bug_hunting"]})
    assert payload["model_profile"] == {"identity": "vendor/coder", "role": "bug_hunter"}
    assert "private-value" not in str(payload)


def test_inspector_follows_live_provider_changes_and_rejects_invalid_mode(inspector):
    api, _ = inspector
    api._provider = ModelConfig(name="workbench", model_id="vendor/selected")
    api._selected_provider_config = lambda: api._provider
    assert api._harness_get("/harness", {})["model_profile"]["identity"] == "vendor/selected"
    api._provider = ModelConfig(name="workbench", model_id="vendor/changed")
    assert api._harness_get("/harness", {"mode": ["red_team"]})["model_profile"]["role"] == "red_team"
    assert api._harness_get("/harness", {})["model_profile"]["identity"] == "vendor/changed"
    with pytest.raises(ValueError):
        api._harness_get("/harness", {"mode": ["invalid"]})


def test_unavailable_local_model_has_no_stale_profile(inspector):
    api, calls = inspector
    api._using_inference = True
    api._selected_provider_config = lambda: None
    payload = api._harness_get("/harness", {})
    assert payload["model_profile"] is None
    assert calls == []


def test_older_profile_without_description_is_optional(inspector, monkeypatch):
    api, _ = inspector
    monkeypatch.setattr("local_coder.harness_api.resolve_profile", lambda *args: object())
    assert api._harness_get("/harness", {})["model_profile"] is None


def test_bug_hunting_assignment_and_active_overlay_match_runtime(inspector):
    api, calls = inspector
    api.provider_profiles = SimpleNamespace(get=lambda name: {"provider": {
        "model_id": "vendor/bug-validator", "api_key": "profile-private-value",
    }})
    (api._harness_root / ".local-coder/bug-models.json").write_text(json.dumps({"exploit": "selected-profile"}))
    api.current_registry = SimpleNamespace(evolution=SimpleNamespace(active_overlay=lambda: {"strategy": "hypothesis_then_test"}))
    payload = api._harness_get("/harness", {"mode": ["bug_hunting"]})
    assert calls[-1] == ("vendor/bug-validator", "bug_hunter", {"strategy": "hypothesis_then_test"})
    assert payload["model_profile"]["identity"] == "vendor/bug-validator"
    assert "profile-private-value" not in str(payload)

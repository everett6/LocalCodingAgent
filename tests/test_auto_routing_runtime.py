"""Automatic routing admission, privacy and budget regressions without network I/O."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from local_coder.core.budgets import BudgetLedger, BudgetedModel
from local_coder.models.manager import ModelManager
from local_coder.types import AgentTask, ModelBackend, ModelConfig, ModelResponse, ProjectConfig, RoutingConfig


class FakeModel:
    def __init__(self, config, reply='{"tier":"economy","confidence":0.99}'):
        self.config = config
        self.reply = reply
        self.calls = []
        self.available = True

    async def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return ModelResponse(content=self.reply)

    async def is_available(self):
        return self.available


def manager_for(**routing):
    config = ProjectConfig(models={
        name: ModelConfig(name=name, model_id=name, base_url="http://localhost:11434")
        for name in ("selected", "cheap", "classifier")
    }, routing=RoutingConfig(economy_model="cheap", **routing))
    config.agentic.role_models = {"explorer": "selected", "coder": "selected", "security": "selected"}
    config.models["classifier"].backend = ModelBackend.OPENAI_COMPAT
    manager = ModelManager(config)
    manager._models = {name: FakeModel(item) for name, item in config.models.items()}
    return manager


def route(manager, objective="Summarize README.md", role="explorer", **kwargs):
    return asyncio.run(manager.route_model(role, objective, **kwargs))


def test_simple_work_routes_without_inference_and_keeps_actual_configuration():
    manager = manager_for()
    model, metadata = route(manager)
    assert model is manager._models["cheap"]
    assert model.config is manager.config.models["cheap"]
    assert metadata["model_slot"] == "economy"
    assert not any(model.calls for model in manager._models.values())


@pytest.mark.parametrize("options,role,kwargs,expected", [
    ({"auto_enabled": False}, "explorer", {}, "selected"),
    ({}, "security", {}, "selected"),
    ({}, "explorer", {"model_name": "classifier"}, "classifier"),
    ({"escalate_to": "classifier"}, "explorer", {"retry": True}, "classifier"),
])
def test_explicit_disabled_protected_and_retry_routes(options, role, kwargs, expected):
    manager = manager_for(**options)
    assert route(manager, role=role, **kwargs)[0] is manager._models[expected]
    assert not any(model.calls for model in manager._models.values())


def test_task_model_override_and_missing_economy_are_preserved():
    manager = manager_for()
    task = AgentTask(role="explorer", objective="Summarize README.md", model_name="classifier")
    assert route(manager, task)[0] is manager._models["classifier"]
    manager.config.routing.economy_model = "absent"
    model, metadata = route(manager)
    assert model is manager._models["selected"]
    assert metadata["reason"] == "economy_unconfigured"


@pytest.mark.parametrize("selected,economy,allow,expected", [
    ("http://localhost:11434", "https://provider.example/v1", True, "selected"),
    ("https://first.example/v1", "https://other.example/v1", False, "selected"),
    ("https://first.example/v1", "https://other.example/v1", True, "cheap"),
    ("https://first.example/v1", "https://first.example/v1/", False, "cheap"),
    ("http://localhost:11434", "http://127.0.0.1:8080/v1", False, "cheap"),
])
def test_provider_boundary(selected, economy, allow, expected):
    manager = manager_for(allow_cross_provider=allow)
    manager.config.models["selected"].base_url = selected
    manager.config.models["cheap"].base_url = economy
    assert route(manager)[0] is manager._models[expected]


def test_verified_tool_incompatibility_blocks_economy(monkeypatch):
    manager = manager_for()
    monkeypatch.setattr("local_coder.models.manager.resolve_profile",
                        lambda *args: SimpleNamespace(capabilities={"tools": False}))
    assert route(manager)[1]["reason"] == "tools_unsupported"


def test_local_classifier_is_bounded_cached_and_uses_shared_budget():
    manager = manager_for(local_classifier="classifier")
    classifier = manager._models["classifier"]
    ledger = BudgetLedger(max_requests=1)
    manager._models["classifier"] = BudgetedModel(classifier, ledger)
    first, metadata = route(manager, "Explain the package layout")
    assert first is manager._models["cheap"]
    assert metadata["source"] == "local_classifier"
    assert ledger.status()["requests"] == 1
    messages, kwargs = classifier.calls[0]
    assert kwargs == {"temperature": 0, "max_tokens": 128, "tools": None}
    assert len(json.loads(messages[1].content)["untrusted_objective"]) <= 2048
    assert route(manager, "Explain the package layout")[1]["cache_hit"] is True
    assert len(classifier.calls) == 1
    # New tasks still pass through the same ledger; exhaustion fails closed.
    assert route(manager, "Explain the module layout")[0] is manager._models["selected"]
    assert len(classifier.calls) == 1


def test_initialized_classifier_inherits_budget_and_quota_guard(monkeypatch):
    manager = manager_for(local_classifier="classifier")
    manager._models.clear()
    manager.budget = BudgetLedger(max_requests=2)
    admissions = []
    guard = SimpleNamespace(admit=lambda config: admissions.append(config.model_id))
    manager.quota_guard = guard
    monkeypatch.setattr(manager, "_create_backend", FakeModel)
    model, metadata = route(manager, "Explain the package layout")
    assert metadata["model_slot"] == "economy"
    assert isinstance(model, BudgetedModel)
    assert manager._models["classifier"].ledger is manager.budget
    assert manager._models["classifier"].quota_guard is guard
    assert admissions == ["classifier"]
    assert manager.budget.status()["requests"] == 1


@pytest.mark.parametrize("url", [
    "https://remote.example/v1", "http://localhost.evil.example", "http://user@localhost/v1",
    "http://localhost/v1?key=value", "http://localhost/#fragment", "http://127.0.0.2/v1",
    "file://localhost/tmp/model", "http://localhost:invalid/v1",
])
def test_remote_or_malformed_classifier_never_receives_task(url):
    manager = manager_for(local_classifier="classifier")
    manager.config.models["classifier"].base_url = url
    assert route(manager, "Explain the package layout")[0] is manager._models["selected"]
    assert manager._models["classifier"].calls == []


@pytest.mark.parametrize("reply", [
    "economy", '{"tier":"economy","confidence":0.5}', RuntimeError("private endpoint error"),
])
def test_classifier_failure_falls_back_without_private_diagnostics(reply):
    manager = manager_for(local_classifier="classifier")
    manager._models["classifier"].reply = reply
    model, metadata = route(manager, "Explain the private package layout")
    assert model is manager._models["selected"]
    assert "private" not in json.dumps(metadata)


def test_anthropic_default_is_hosted_even_when_configuration_looks_local():
    manager = manager_for(local_classifier="classifier")
    manager.config.models["classifier"].backend = ModelBackend.ANTHROPIC
    assert route(manager, "Explain the package layout")[1]["reason"] == "invalid_local_classifier"
    assert manager._models["classifier"].calls == []


def test_ollama_classifier_is_rejected_without_inference():
    manager = manager_for(local_classifier="classifier")
    manager.config.models["classifier"].backend = ModelBackend.OLLAMA
    assert route(manager, "Explain the package layout")[1]["reason"] == "invalid_local_classifier"
    assert manager._models["classifier"].calls == []


@pytest.mark.parametrize("backend", [ModelBackend.OPENAI_COMPAT, ModelBackend.LLAMACPP])
def test_local_classifier_transport_disables_environment_proxy_and_redirects(monkeypatch, backend):
    manager = manager_for(local_classifier="classifier")
    captured = []
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8000")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8000")
    monkeypatch.setenv("NO_PROXY", "")

    def client(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("local_coder.models.openai_compat.httpx.AsyncClient", client)
    config = manager.config.models["classifier"]
    config.backend = backend
    manager._create_backend(config)
    assert captured[0]["trust_env"] is False
    assert captured[0]["follow_redirects"] is False


def test_classifier_timeout_is_cancelled_without_retries():
    manager = manager_for(local_classifier="classifier", classifier_timeout_seconds=0.25)
    cancelled = []

    async def slow(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    manager._models["classifier"].generate = slow
    assert route(manager, "Explain the package layout")[1]["reason"] == "local_classifier_failed"
    assert cancelled == [True]


def test_cache_includes_context_is_bounded_and_contains_no_raw_tasks():
    manager = manager_for()
    task = AgentTask(role="explorer", objective="Summarize README.md")
    assert route(manager, task)[0] is manager._models["cheap"]
    task.constraints = ["Check authorization vulnerabilities"]
    assert route(manager, task)[0] is manager._models["selected"]
    for index in range(140):
        route(manager, f"Summarize file{index}.md")
    assert len(manager._routing_cache) == 128
    assert "Summarize" not in repr(manager._routing_cache)


def test_economy_fallback_cannot_bypass_local_provider_boundary():
    manager = manager_for(fallbacks={"cheap": ["classifier"]}, allow_cross_provider=True)
    manager._models["cheap"].available = False
    manager.config.models["classifier"].base_url = "https://remote.example/v1"
    model, metadata = route(manager)
    assert model is manager._models["selected"]
    assert metadata["tier"] == "selected"


@pytest.mark.parametrize("price", [100, None])
def test_economy_fallback_cannot_silently_escalate_price_or_unknown_cost(price):
    manager = manager_for(fallbacks={"cheap": ["classifier"]})
    for name, cost in (("selected", 1), ("cheap", .1), ("classifier", price)):
        item = manager.config.models[name]
        item.backend = ModelBackend.OPENAI_COMPAT
        item.base_url = "https://openrouter.ai/api/v1"
        item.input_cost_per_million = item.output_cost_per_million = cost
    manager._models["cheap"].available = False
    model, metadata = route(manager)
    assert model is manager._models["selected"]
    assert metadata["tier"] == metadata["model_slot"] == "selected"


def test_known_cheaper_same_provider_fallback_remains_available():
    manager = manager_for(fallbacks={"cheap": ["classifier"]})
    for name, cost in (("selected", 1), ("cheap", .1), ("classifier", .2)):
        item = manager.config.models[name]
        item.backend = ModelBackend.OPENAI_COMPAT
        item.base_url = "https://openrouter.ai/api/v1"
        item.input_cost_per_million = item.output_cost_per_million = cost
    manager._models["cheap"].available = False
    assert route(manager)[0] is manager._models["classifier"]

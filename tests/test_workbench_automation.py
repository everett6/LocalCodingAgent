"""Cross-layer setup behavior, with no provider inference calls."""
import time

from local_coder.providers.quota import ProviderQuotaExhausted
from local_coder.remote import RemoteControlServer
from local_coder.types import AgentEvent, AgentMode


def test_catalog_metadata_and_mode_policy_apply_to_actual_coordinator(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    monkeypatch.setattr(server.catalog_service, "cached_model", lambda _: {
        "context_length": 262144, "max_output_tokens": 8192,
        "input_cost_per_million": 0, "output_cost_per_million": 0, "request_cost_usd": 0,
    })
    code, result = server.handle("POST", "/settings", {
        "base_url": "https://openrouter.ai/api/v1", "model_id": "example/model:free",
        "context_length": 4096, "auto_context": True, "mode": "red_team",
    })
    assert code == 200 and result["provider"]["context_length"] == 262144
    assert result["context_policy"]["mode"] == "red_team"
    server._context.mode = AgentMode.RED_TEAM
    coordinator = server._coordinator()
    assert coordinator.config.mode == AgentMode.RED_TEAM
    assert coordinator.config.agentic.context_window_chars == result["context_policy"]["context_window_chars"]
    assert coordinator.config.harness.memory_context_chars == result["context_policy"]["memory_chars"]
    assert coordinator.model_manager.quota_guard is server.free_quota_guard
    assert coordinator.config.models["workbench"].max_tokens == result["context_policy"]["completion_tokens"]


def test_explicit_environment_profile_does_not_reuse_previous_raw_key(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    common = {"base_url": "https://api.example.com/v1", "model_id": "model"}
    assert server.handle("POST", "/settings", {**common, "api_key": "old-private"})[0] == 200
    monkeypatch.setenv("PROFILE_KEY", "new-private")
    code, result = server.handle("POST", "/settings", {**common, "api_key_env": "PROFILE_KEY", "auto_context": False})
    assert code == 200 and result["provider"]["auto_context"] is False
    assert server._provider.resolve_api_key() == "new-private"
    assert "new-private" not in str(result) and "old-private" not in str(result)


def test_quota_exception_pauses_job_and_keeps_checkpoint(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    class FakeCoordinator:
        async def run(self, request, **kwargs):
            server._event(AgentEvent(source="CODER", event_type="checkpoint_saved", message="Saved", data={"path": ".local-coder/checkpoint_test.json"}))
            raise ProviderQuotaExhausted({"message": "Daily allowance exhausted", "remaining": 0})
        class Manager:
            async def close_all(self):
                pass
        model_manager = Manager()
    monkeypatch.setattr(server, "_coordinator", lambda: FakeCoordinator())
    code, job = server.handle("POST", "/jobs", {"request": "Review local code"})
    assert code == 202
    for _ in range(200):
        record = server.sessions.get(job["session_id"])
        if record["status"] not in {"queued", "running"}:
            break
        time.sleep(.01)
    assert record["status"] == "quota_exhausted"
    assert record["checkpoint"] == ".local-coder/checkpoint_test.json"
    assert record["quota_notice"]["remaining"] == 0


def test_paid_quota_resume_requires_spending_limit_before_start(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    assert server.handle("POST", "/settings", {"base_url": "https://openrouter.ai/api/v1", "model_id": "explicit/paid"})[0] == 200
    code, _ = server.handle("POST", "/jobs", {"request": "Resume", "resume_checkpoint": ".local-coder/checkpoint_test.json", "allow_model_change": True})
    assert code == 400
    assert server.sessions.list() == []


def test_on_device_provider_has_runtime_context_and_no_provider_charges(tmp_path, monkeypatch):
    from local_coder.core.budgets import BudgetLedger
    from local_coder.types import Message
    server = RemoteControlServer(str(tmp_path))
    monkeypatch.setattr(server.inference, "endpoint_config", lambda: {"base_url": "http://127.0.0.1:1234/v1", "model": "local", "api_key": "runtime-only"})
    monkeypatch.setattr(server.inference, "status", lambda: {"settings": {"context_size": 32768}})
    model = server.local_inference_provider()
    assert model.context_source == "runtime"
    ledger = BudgetLedger(max_cost_usd=.01)
    ledger.reserve(model, [Message(role="user", content="Review local code")], 100)
    assert ledger.status()["cost_usd"] == 0

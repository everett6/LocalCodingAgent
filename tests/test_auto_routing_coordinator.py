"""Prove cheap read routing avoids the expensive orchestration stages."""
import asyncio
from types import SimpleNamespace

from local_coder.orchestrator.coordinator import Coordinator
from local_coder.orchestrator.config_loader import load_config
from local_coder.types import AgentResponse, AgentRole, ModelConfig, ProjectConfig, RoutingConfig, SafetyTier, TaskStatus


def test_direct_read_uses_economy_and_skips_planner_writer_and_reviewer(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("Small local fixture.")
    models = {name: ModelConfig(name=name, model_id=name) for name in ("default", "cheap")}
    config = ProjectConfig(models=models, routing=RoutingConfig(economy_model="cheap"),
                           safety_tier=SafetyTier.TIER_1_PARANOIA)
    instance = Coordinator(config, str(tmp_path))
    instance.model_manager._models = {name: SimpleNamespace(config=model) for name, model in models.items()}
    seen, events = [], []
    instance.on_event(events.append)

    class Reader:
        async def execute(self, task):
            assert task.role == AgentRole.EXPLORER
            return AgentResponse(task_id=task.task_id, status=TaskStatus.COMPLETED, summary="Observed fixture summary")

    def agent(role, model, *args, **kwargs):
        seen.append((role, model.config.name))
        return Reader()

    async def forbidden(*args, **kwargs):
        raise AssertionError("A direct read must not invoke extra model stages")

    monkeypatch.setattr("local_coder.orchestrator.coordinator.create_agent", agent)
    instance._plan = instance._explore = instance._review = forbidden
    result = asyncio.run(instance.run("Summarize README.md"))
    assert result == "Observed fixture summary"
    assert seen == [(AgentRole.EXPLORER, "cheap")]
    routing = [event for event in events if event.event_type == "routing_decision"]
    assert len(routing) == 1 and routing[0].data["model_slot"] == "economy"
    assert "README.md" not in str(routing[0].data)


def test_routing_yaml_roundtrip_and_conservative_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "absent.yaml"))
    (tmp_path / ".local-coder.yaml").write_text("""routing:
  auto_enabled: true
  economy_model: economy
  local_classifier: tiny
  classifier_timeout_seconds: 1.0
  allow_cross_provider: false
""")
    config = load_config(project_root=str(tmp_path))
    assert config.routing.economy_model == "economy"
    assert config.routing.local_classifier == "tiny"
    assert config.routing.classifier_timeout_seconds == 1.0
    assert config.routing.allow_cross_provider is False

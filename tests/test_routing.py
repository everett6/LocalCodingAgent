"""Multi-model routing: role map, escalation, fallbacks, and tool settings."""
import asyncio
from types import SimpleNamespace

import pytest

from local_coder.models.manager import ModelManager
from local_coder.models.router import fallback_chain, resolve_model_name
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.tools import create_tool_registry
from local_coder.types import (
    AgentRole, AgentTask, ModelConfig, ModelResponse, ProjectConfig, RoutingConfig, TaskPlan,
    TaskStatus, ToolName, ToolSettings,
)


def make_config(tmp_path=None, **routing) -> ProjectConfig:
    config = ProjectConfig(
        models={
            "fast": ModelConfig(name="fast", model_id="small"),
            "strong": ModelConfig(name="strong", model_id="big"),
        },
        routing=RoutingConfig(**routing),
        project_root=str(tmp_path or "."),
    )
    config.agentic.role_models = {"coder": "fast", "planner": "strong"}
    return config


def test_resolve_model_name_order():
    config = make_config(escalate_to="strong")

    assert resolve_model_name(config, AgentRole.CODER) == "fast"
    assert resolve_model_name(config, AgentRole.CODER, escalate=True) == "strong"
    # An explicit per-task model beats escalation and the role map.
    assert resolve_model_name(config, AgentRole.CODER, "fast", escalate=True) == "fast"
    # Unmapped role falls back to the first model.
    assert resolve_model_name(config, AgentRole.REVIEWER) == "fast"
    with pytest.raises(ValueError):
        resolve_model_name(config, AgentRole.CODER, "missing")


def test_escalate_without_escalate_to_uses_role_map():
    assert resolve_model_name(make_config(), AgentRole.CODER, escalate=True) == "fast"


def test_fallback_chain_skips_unknown_and_duplicate_models():
    config = make_config(fallbacks={"strong": ["fast", "missing", "strong", "fast"]})

    assert fallback_chain(config, "strong") == ["strong", "fast"]
    assert fallback_chain(config, "fast") == ["fast"]


class StubModel:
    def __init__(self, name, available=True):
        self.config = SimpleNamespace(name=name, temperature=0.0, max_tokens=100)
        self.available = available
        self.probes = 0

    async def is_available(self):
        self.probes += 1
        return self.available

    async def generate(self, messages, **kwargs):
        return ModelResponse(content="done")


def test_manager_uses_fallback_when_primary_is_unreachable():
    config = make_config(fallbacks={"strong": ["fast"]})
    manager = ModelManager(config)
    strong, fast = StubModel("strong", available=False), StubModel("fast")
    manager._models = {"strong": strong, "fast": fast}

    async def go():
        first = await manager.get_model(AgentRole.PLANNER)
        second = await manager.get_model(AgentRole.PLANNER)
        return first, second

    first, second = asyncio.run(go())

    assert first is fast and second is fast
    assert strong.probes == 1, "reachability is probed once, then cached"


def test_manager_reports_when_every_configured_model_is_unreachable():
    config = make_config(fallbacks={"strong": ["fast"]})
    manager = ModelManager(config)
    manager._models = {
        "strong": StubModel("strong", available=False),
        "fast": StubModel("fast", available=False),
    }

    with pytest.raises(RuntimeError, match="No reachable model.*strong, fast"):
        asyncio.run(manager.get_model(AgentRole.PLANNER))


def test_manager_does_not_probe_models_without_fallbacks():
    manager = ModelManager(make_config())
    fast = StubModel("fast", available=False)
    manager._models = {"strong": StubModel("strong"), "fast": fast}

    assert asyncio.run(manager.get_model(AgentRole.CODER)) is fast
    assert fast.probes == 0


def test_unreachable_primary_can_recover_after_negative_cache_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("local_coder.models.manager.monotonic", lambda: clock[0])
    manager = ModelManager(make_config(fallbacks={"strong": ["fast"]}))
    strong, fast = StubModel("strong", available=False), StubModel("fast")
    manager._models = {"strong": strong, "fast": fast}

    async def go():
        assert await manager.get_model(AgentRole.PLANNER) is fast
        strong.available = True
        clock[0] += 1
        assert await manager.get_model(AgentRole.PLANNER) is fast
        assert strong.probes == 1
        clock[0] += 5
        assert await manager.get_model(AgentRole.PLANNER) is strong
        clock[0] += 100
        assert await manager.get_model(AgentRole.PLANNER) is strong

    asyncio.run(go())
    assert strong.probes == 2 and fast.probes == 1


def test_unreachable_probe_failures_have_bounded_retry_rate(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("local_coder.models.manager.monotonic", lambda: clock[0])
    manager = ModelManager(make_config(fallbacks={"strong": ["fast"]}))
    calls = []

    async def failing():
        calls.append(True)
        raise RuntimeError("fixture unavailable")

    manager._models = {"strong": SimpleNamespace(is_available=failing), "fast": StubModel("fast")}

    async def go():
        for _ in range(20):
            assert await manager.get_model(AgentRole.PLANNER) is manager._models["fast"]
        assert len(calls) == 1
        clock[0] += 5
        for _ in range(20):
            await manager.get_model(AgentRole.PLANNER)
        assert len(calls) == 2

    asyncio.run(go())


def test_simultaneous_fallback_checks_share_one_probe(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("local_coder.models.manager.monotonic", lambda: clock[0])
    manager = ModelManager(make_config(fallbacks={"strong": ["fast"]}))
    calls = []

    async def unavailable():
        calls.append(True)
        await asyncio.sleep(0)
        return False

    manager._models = {"strong": SimpleNamespace(is_available=unavailable), "fast": StubModel("fast")}

    async def go():
        for _ in range(2):
            results = await asyncio.gather(*(manager.get_model(AgentRole.PLANNER) for _ in range(8)))
            assert all(result is manager._models["fast"] for result in results)
            clock[0] += 5

    asyncio.run(go())
    assert len(calls) == 2


def test_cancelled_reachability_probe_releases_waiters_without_negative_cache():
    manager = ModelManager(make_config(fallbacks={"strong": ["fast"]}))

    async def go():
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        manager._models = {"strong": SimpleNamespace(is_available=blocked), "fast": StubModel("fast")}
        first = asyncio.create_task(manager.get_model(AgentRole.PLANNER))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        recovered = StubModel("strong")
        manager._models["strong"] = recovered
        assert await manager.get_model(AgentRole.PLANNER) is recovered
        assert recovered.probes == 1

    asyncio.run(go())


def test_closing_manager_clears_reachability_before_new_backends_are_loaded():
    manager = ModelManager(make_config(fallbacks={"strong": ["fast"]}))

    async def go():
        old = StubModel("strong")
        manager._models = {"strong": old, "fast": StubModel("fast")}
        assert await manager.get_model(AgentRole.PLANNER) is old
        await manager.close_all()
        new, fallback = StubModel("strong", available=False), StubModel("fast")
        manager._models = {"strong": new, "fast": fallback}
        assert await manager.get_model(AgentRole.PLANNER) is fallback
        assert new.probes == 1

    asyncio.run(go())


def test_failed_plan_task_is_retried_on_escalation_model(tmp_path):
    coordinator = Coordinator(config=make_config(tmp_path, escalate_to="strong"), project_root=str(tmp_path))
    calls = []

    async def get_model(role, model_name=None, *, escalate=False):
        calls.append(escalate)
        if not escalate:
            raise RuntimeError("fast model choked")
        return StubModel("strong")

    coordinator.model_manager.get_model = get_model
    plan = TaskPlan(objective="x", tasks=[
        AgentTask(task_id="a", role=AgentRole.CODER, objective="x", files=["a.py"]),
        AgentTask(task_id="b", role=AgentRole.CODER, objective="y", files=["b.py"]),
    ])

    result = asyncio.run(coordinator._execute_plan(plan))

    assert result.status == TaskStatus.COMPLETED
    assert calls.count(True) == 2


def test_no_retry_when_task_already_runs_on_escalation_model(tmp_path):
    config = make_config(tmp_path, escalate_to="fast")  # coder already routes to fast
    coordinator = Coordinator(config=config, project_root=str(tmp_path))
    calls = []

    async def get_model(role, model_name=None, *, escalate=False):
        calls.append(escalate)
        raise RuntimeError("down")

    coordinator.model_manager.get_model = get_model
    plan = TaskPlan(objective="x", tasks=[
        AgentTask(task_id="a", role=AgentRole.CODER, objective="x"),
        AgentTask(task_id="b", role=AgentRole.CODER, objective="y", depends_on=["a"]),
    ])

    result = asyncio.run(coordinator._execute_plan(plan))

    assert result.status == TaskStatus.FAILED
    assert calls == [False]


def test_second_fix_attempt_escalates(tmp_path):
    config = make_config(tmp_path, escalate_to="strong")
    config.verification.max_fix_iterations = 2
    coordinator = Coordinator(config=config, project_root=str(tmp_path))
    escalations = []

    async def get_model(role, model_name=None, *, escalate=False):
        if role == AgentRole.DEBUGGER:
            escalations.append(escalate)
        return StubModel("x")

    async def failing_tests():
        from local_coder.types import AgentResponse
        return AgentResponse(task_id="t", status=TaskStatus.COMPLETED, summary="FAILED", tests_passed=False)

    coordinator.model_manager.get_model = get_model
    coordinator._run_tests = failing_tests
    asyncio.run(coordinator.run("fix it"))

    assert escalations == [False, True]


def test_tool_settings_apply_to_registry(tmp_path):
    registry = create_tool_registry(str(tmp_path))
    registry.apply_settings(ToolSettings(
        disabled=["git_commit", "unknown_tool"],
        command_timeout=222,
        roles={"reviewer": ["read_file", "grep", "unknown_tool"], "nonsense": ["read_file"]},
    ))

    assert registry.get_tool(ToolName.GIT_COMMIT) is None
    assert not registry.has_permission(AgentRole.CODER, ToolName.GIT_COMMIT)
    assert {t.name for t in registry.get_tools_for_role(AgentRole.REVIEWER)} == {ToolName.READ_FILE, ToolName.GREP}
    assert registry.get_tool(ToolName.RUN_COMMAND).default_timeout == 222


def test_coordinator_applies_tool_settings(tmp_path):
    config = make_config(tmp_path)
    config.tools = ToolSettings(disabled=["run_command"])

    coordinator = Coordinator(config=config, project_root=str(tmp_path))

    assert coordinator.tool_registry.get_tool(ToolName.RUN_COMMAND) is None


def test_openai_backend_sends_bearer_key_only_when_set():
    from local_coder.models.openai_compat import OpenAICompatibleBackend

    keyed = OpenAICompatibleBackend(ModelConfig(name="h", model_id="m", api_key="sk-x"))
    plain = OpenAICompatibleBackend(ModelConfig(name="p", model_id="m"))

    assert keyed.client.headers["Authorization"] == "Bearer sk-x"
    assert "Authorization" not in plain.client.headers

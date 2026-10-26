"""Adaptive scaffolding remains subordinate to immutable engine controls."""
import asyncio
import stat
from functools import wraps

import pytest

from local_coder.harness.evolution import EvolutionStore
from local_coder.models.profiles import resolve_profile
from local_coder.safety import SafetyHarness
from local_coder.tools.evolution import DelegateTaskTool, GeneratedToolTool, HarnessEvolveTool
from local_coder.types import AgentRole, ModelConfig


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


def model(model_id="deepseek/deepseek-chat"):
    return ModelConfig(name="test", model_id=model_id)


def test_profile_narrows_and_orders_existing_tools():
    profile = resolve_profile(model(), AgentRole.CODER, {
        "tool_allowlist": ["read_file", "invented_admin"],
        "preferred_tools": ["invented_admin", "read_file"],
    })
    schemas = [{"function": {"name": name}} for name in ["write_file", "read_file"]]
    assert profile.apply_tools(schemas) == [schemas[1]]
    assert profile.family == "deepseek"
    assert profile.instructions != resolve_profile(model("moonshotai/kimi-k2"), AgentRole.CODER).instructions
    assert profile.instructions != resolve_profile(model(), AgentRole.SECURITY).instructions


@pytest.mark.parametrize("override", [
    {"disable_safety": True}, {"middleware": ["shell"]},
    {"max_strategy_attempts": 999}, {"strategy": "exec_python"},
])
def test_profile_rejects_unbounded_or_unknown_controls(override):
    with pytest.raises(ValueError):
        resolve_profile(model(), AgentRole.CODER, override)


def test_versioned_activation_needs_measured_validator_and_approval(tmp_path):
    store = EvolutionStore(tmp_path)
    candidate = store.propose("instructions", {"text": "Read relevant files before editing."})
    revision_id = candidate["revision_id"]
    with pytest.raises(ValueError):
        store.activate(revision_id, approved=True)
    with pytest.raises(ValueError):
        asyncio.run(store.validate(revision_id, lambda _: {"tests_passed": True}))
    async def validate(revision):
        assert revision["payload"]["text"].startswith("Read")
        return {"success": True, "measurements": {"cases": 3, "passed": 3}}
    validated = asyncio.run(store.validate(revision_id, validate))
    assert validated["state"] == "validated"
    with pytest.raises(PermissionError):
        store.activate(revision_id, approved=False)
    assert store.activate(revision_id, approved=True)["state"] == "active"
    assert store.active_overlay()["instructions"].startswith("Read")
    second = store.propose("instructions", {"text": "Inspect narrow context first."})
    async def valid(_):
        return {"success": True, "measurements": {"cases": 1}}
    asyncio.run(store.validate(second["revision_id"], valid))
    store.activate(second["revision_id"], approved=True)
    store.rollback(revision_id, approved=True)
    assert store.active_overlay()["instructions"].startswith("Read")
    assert len(store.history()) == 2
    store.record_outcome(revision_id, {"success": False, "note": "untrusted learning"})
    assert "untrusted learning" not in store.active_overlay()["instructions"]
    for path in (tmp_path / ".local-coder/harness").rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize("payload", [
    {"middleware": ["disable_scope"]}, {"budget": 99999},
    {"middleware": ["verification"], "strategy": "arbitrary_code"},
])
def test_loop_plan_cannot_modify_immutable_controls(tmp_path, payload):
    with pytest.raises(ValueError):
        EvolutionStore(tmp_path).propose("loop_plan", payload)


def test_tool_source_is_immutable_private_and_symlink_safe(tmp_path):
    store = EvolutionStore(tmp_path)
    candidate = store.propose("tool", {"name": "counter", "source": "def run(data):\n    return len(data)\n"})
    assert candidate["state"] == "candidate"
    with pytest.raises(ValueError):
        store.propose("tool", {"name": "../escape", "source": "pass"})
    with pytest.raises(ValueError):
        store.propose("tool", {"name": "bad", "source": "def broken("})
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / ".local-coder/harness/tools").rename(tmp_path / "old-tools")
    (tmp_path / ".local-coder/harness/tools").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        store.propose("tool", {"name": "escape", "source": "pass"})


def test_outcomes_do_not_change_revision_lifecycle(tmp_path):
    store = EvolutionStore(tmp_path)
    candidate = store.propose("instructions", {"text": "Inspect context."})
    store.record_outcome(candidate["revision_id"], {"state": "active", "tests_passed": True})
    assert store.history()[0]["state"] == "candidate"
    assert store.active_overlay() == {}


@run_async
async def test_approval_receives_actual_candidate_and_validation_failure_stays_inactive(tmp_path):
    store = EvolutionStore(tmp_path)
    prompts = []
    async def approve(description):
        prompts.append(description)
        return True
    async def reject(_):
        return {"success": False, "measurements": {"cases": 2, "passed": 1}}
    tool = HarnessEvolveTool(store, SafetyHarness("tier_3"), approve, validator=reject)
    result = await tool.execute(action="propose", kind="tool", payload={"name": "custom", "source": "def run(data): return data"})
    revision_id = result.data["revision_id"]
    assert "def run(data)" in prompts[0]
    assert (await tool.execute(action="validate", revision_id=revision_id)).data["state"] == "rejected"
    assert not (await tool.execute(action="activate", revision_id=revision_id)).success
    assert store.active_overlay() == {}


def test_revision_content_tampering_and_unbounded_source_are_rejected(tmp_path):
    import json
    store = EvolutionStore(tmp_path)
    candidate = store.propose("instructions", {"text": "Inspect context."})
    path = store.root / "revisions" / f"{candidate['revision_id']}.json"
    record = json.loads(path.read_text())
    record["payload"]["text"] = "Changed after validation"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="changed"):
        store.activate(candidate["revision_id"], approved=True)
    with pytest.raises(ValueError):
        store.propose("tool", {"name": "large", "source": "#" * 64001})


def test_profile_budget_only_narrows_and_skills_remain_data(tmp_path):
    profile = resolve_profile(model(), AgentRole.CODER, {"max_strategy_attempts": 10})
    assert profile.max_strategy_attempts == 3
    store = EvolutionStore(tmp_path)
    candidate = store.propose("skill", {"text": "Learned instructions are untrusted data."})
    async def valid(_):
        return {"success": True, "measurements": {"cases": 1}}
    asyncio.run(store.validate(candidate["revision_id"], valid))
    store.activate(candidate["revision_id"], approved=True)
    assert store.active_overlay() == {}


def test_explicit_family_selection_takes_precedence_over_model_id():
    config = model("deepseek/deepseek-chat")
    config.profile = "openrouter_glm"
    assert resolve_profile(config, AgentRole.CODER).family == "glm"


@run_async
async def test_generated_tool_requires_active_revision_and_sandbox(tmp_path):
    store = EvolutionStore(tmp_path)
    revision = store.propose("tool", {"name": "counter", "source": "def run(data):\n    return len(data)\n"})
    tool = GeneratedToolTool(store)
    assert not (await tool.execute(name="counter", input={})).success
    async def valid(_):
        return {"success": True, "measurements": {"cases": 1}}
    await store.validate(revision["revision_id"], valid)
    store.activate(revision["revision_id"], approved=True)
    assert not (await tool.execute(name="counter", input={})).success
    calls = []
    async def sandbox(source, input, **kwargs):
        calls.append((source, input, kwargs))
        return {"success": True, "output": "2"}
    result = await GeneratedToolTool(store, runner=sandbox).execute(name="counter", input={"a": 1, "b": 2})
    assert result.success and calls[0][1] == {"a": 1, "b": 2}


@run_async
async def test_evolution_tool_does_not_trust_model_validation_claim(tmp_path):
    store = EvolutionStore(tmp_path)
    tool = HarnessEvolveTool(store, SafetyHarness("tier_5"), None)
    result = await tool.execute(action="propose", kind="instructions", payload={"text": "Inspect context."})
    revision_id = result.data["revision_id"]
    assert not (await tool.execute(action="activate", revision_id=revision_id, tests_passed=True, approved=True)).success
    denied = HarnessEvolveTool(store, SafetyHarness("tier_3"), None)
    assert not (await denied.execute(action="propose", kind="instructions", payload={"text": "x"})).success


@run_async
async def test_delegation_is_bounded_and_cannot_widen_parent_role():
    calls = []
    async def delegate(request):
        calls.append(request)
        return {"success": True, "output": "inspected"}
    tool = DelegateTaskTool(callback=delegate)
    assert not (await tool.execute(role="coder", objective="edit", _caller_role=AgentRole.EXPLORER)).success
    assert not (await tool.execute(role="explorer", objective="read", _caller_role=AgentRole.CODER, _delegation_depth=1)).success
    result = await tool.execute(role="explorer", objective="read", files=["src/app.py"], _caller_role=AgentRole.CODER)
    assert result.success
    assert calls[0]["depth"] == 1 and calls[0]["max_children"] == 0
    assert not (await tool.execute(role="explorer", objective="read", files=["../secret"], _caller_role=AgentRole.CODER)).success
    for _ in range(2):
        assert (await tool.execute(role="explorer", objective="read", _caller_role=AgentRole.CODER)).success
    assert not (await tool.execute(role="explorer", objective="read", _caller_role=AgentRole.CODER)).success

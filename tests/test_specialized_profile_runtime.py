"""Specialized scaffolds change per turn without changing configured ceilings."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

from local_coder.agents.base import BaseAgent
from local_coder.context.working_memory import WORKING_MEMORY_PREFIX
from local_coder.types import AgentRole, AgentTask, Message, ModelResponse, TaskStatus, ToolCall


class Registry:
    def __init__(self):
        self.profile_overrides = {}
        self.memory_context_chars = 10000

    def get_schemas_for_role(self, role):
        return []


class Agent(BaseAgent):
    role = AgentRole.CODER
    system_prompt = "Keep engine permissions and scope."


def make_agent(**kwargs):
    model = SimpleNamespace(config=SimpleNamespace(model_id="vendor/unknown-one", strategy_attempt_limit=3))
    registry = Registry()
    agent = Agent(model, registry, **kwargs)
    task = AgentTask(role=AgentRole.CODER, objective="Inspect and verify the parser")
    return agent, registry, task


def refresh(agent, task, messages=None):
    if messages is None:
        messages = agent._build_messages(task)
    return asyncio.run(agent._runtime_context(messages, task))


def engine_limits(monkeypatch, registry):
    """Supply engine-owned limits without opening them to user overlays."""
    import local_coder.models.profiles as profiles

    original = profiles.resolve_profile
    registry.profile_limits = {}

    def resolve(*args, **kwargs):
        return replace(original(*args, **kwargs), **registry.profile_limits)

    monkeypatch.setattr(profiles, "resolve_profile", resolve)


def test_model_and_overlay_switches_replace_guidance_and_emit_only_on_changes():
    events = []
    agent, registry, task = make_agent(event_callback=events.append)
    original = agent._build_messages(task)
    first = refresh(agent, task, original)
    second = refresh(agent, task, first)
    assert len([event for event in events if event.event_type == "profile_selected"]) == 1

    # Unknown identities still represent different model selections internally;
    # their arbitrary provider strings must never be emitted as metadata.
    agent.model.config.model_id = "vendor/private-secret-model-two"
    third = refresh(agent, task, second)
    registry.profile_overrides = {"instructions": "Inspect a focused reproduction, then verify the fix."}
    fourth = refresh(agent, task, third)
    agent.role = AgentRole.DEBUGGER
    fifth = refresh(agent, task, fourth)
    selected = [event for event in events if event.event_type == "profile_selected"]
    assert len(selected) == 4
    assert "private-secret-model-two" not in json.dumps([event.data for event in selected])
    assert original == agent._build_messages(task)
    runtime = [message for message in fifth if message.content.startswith("Runtime model profile")]
    assert len(runtime) == 1 and runtime[0].role == "user"
    assert "focused reproduction" in runtime[0].content
    assert len([message for message in fifth if message.content.startswith(WORKING_MEMORY_PREFIX)]) == 1


def test_profile_retrieval_share_reduces_user_budget_and_recovers_on_switch(monkeypatch):
    class Memory:
        def __init__(self):
            self.budgets = []

        def context(self, query, budget, task_id, **kwargs):
            self.budgets.append(budget)
            return {"text": "", "items": []}

    agent, registry, task = make_agent()
    engine_limits(monkeypatch, registry)
    registry.context_memory = Memory()
    registry.profile_limits = {"context_share_percent": 5}
    first = refresh(agent, task)
    assert registry.context_memory.budgets[-1] == agent._message_budget() * 5 // 100
    registry.profile_limits = {"context_share_percent": 20}
    refresh(agent, task, first)
    assert registry.context_memory.budgets[-1] == agent._message_budget() * 20 // 100
    registry.memory_context_chars = 300
    refresh(agent, task, first)
    assert registry.context_memory.budgets[-1] == 300


def test_profile_tool_result_cap_applies_to_paged_output_without_shrinking_user_limit(monkeypatch):
    agent, registry, task = make_agent(max_tool_output_chars=8000)
    engine_limits(monkeypatch, registry)
    registry.profile_limits = {"tool_result_char_limit": 1000}
    registry.profile_overrides = {"max_strategy_attempts": 1}
    first = refresh(agent, task)
    assert agent._tool_output_limits()[0] == 1000
    assert agent.strategy_attempt_limit == 1
    output = "evidence for the reproduction\n" * 300
    call = ToolCall(id="read", name="read_file")
    fitted = agent._fit_read_file(call, output)
    assert len(fitted) <= 1000 and "start_line=" in fitted
    archived = agent._fit_tool_output(call, output)
    assert archived.startswith(fitted) and "Full untrusted tool output archived:" in archived
    assert agent.max_tool_output_chars == 8000
    registry.profile_limits = {"tool_result_char_limit": 16000}
    registry.profile_overrides = {}
    refresh(agent, task, first)
    assert agent._tool_output_limits()[0] == 8000
    assert agent.strategy_attempt_limit == 3
    agent.max_tool_output_chars = 500
    assert agent._tool_output_limits()[0] == 500


def test_catalog_profile_cap_never_enlarges_physical_budget(monkeypatch):
    import local_coder.context.catalog as catalog

    selected_budgets = []
    original = catalog.select_tool_catalog

    def select(*args, **kwargs):
        selected_budgets.append(kwargs["char_budget"])
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog, "select_tool_catalog", select)
    agent, registry, task = make_agent()
    engine_limits(monkeypatch, registry)
    registry.profile_limits = {"catalog_char_limit": 1200}
    first = refresh(agent, task)
    assert selected_budgets[-1] == 1200
    agent.context_window_chars = 900
    refresh(agent, task, first)
    assert selected_budgets[-1] == 900


def test_workflow_precedes_optional_journal_with_tight_guidance_budget():
    agent, registry, task = make_agent(context_window_chars=2400)
    instructions = "Inspect the failing boundary; reproduce it; apply a focused fix; verify observable behavior. " * 5
    registry.profile_overrides = {"instructions": instructions}
    agent._progress_journal = [{"success": False, "observation": "long optional journal " * 60}]
    messages = refresh(agent, task)
    runtime = next(message.content for message in messages if message.content.startswith("Runtime model profile"))
    assert instructions in runtime
    assert len(runtime) < 1700


def test_iteration_admission_uses_refreshed_message_budget():
    agent, registry, task = make_agent()
    calls = []

    async def generate(*args, **kwargs):
        calls.append(True)
        return ModelResponse(content="Completed")

    async def changed_runtime_context(messages, task, **kwargs):
        # A provider/profile switch changes the available prompt envelope.
        agent.context_window_chars = 400
        return [Message(role="system", content=agent.system_prompt), Message(role="user", content="x" * 1000)]

    agent.model.generate = generate
    agent._runtime_context = changed_runtime_context
    agent._needs_compaction = lambda messages: False
    result = asyncio.run(agent.execute(task))
    assert result.status == TaskStatus.FAILED
    assert result.issues == ["Context budget exceeded"]
    assert calls == []

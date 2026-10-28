"""Bounded recovery respects refusals and retains exact protocol/evidence."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from local_coder.agents.base import BaseAgent, PINNED_PREFIX
from tests.test_agents import fake_schemas
from local_coder.agents.frame_guard import detect_refusal
from local_coder.context.compression import prune_tool_outputs, sliding_tool_output_budget
from local_coder.types import AgentRole, AgentTask, Message, ModelResponse, TaskStatus, ToolCall, ToolResult


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.config = SimpleNamespace(temperature=.1, max_tokens=100, context_length=32000)

    async def generate(self, messages, **kwargs):
        self.calls.append((list(messages), kwargs))
        return next(self.responses)


class Registry:
    def __init__(self):
        self.calls = []

    def get_schemas_for_role(self, role):
        return fake_schemas("read_file")

    async def execute_tool(self, role, name, arguments):
        self.calls.append(name)
        return ToolResult(success=True, output="ok")


class Agent(BaseAgent):
    role = AgentRole.SECURITY
    system_prompt = "Review the authorized local source."


@pytest.mark.parametrize("text", [
    "I cannot fulfill this request.",
    "I'm sorry, but I can't help with that request.",
    "As an AI assistant, I am unable to provide an exploit payload.",
    "As an AI language model, I cannot fulfill this request.",
    "I reviewed the source. I cannot fulfill this request.",
])
def test_explicit_assistant_refusal_is_a_structured_safe_stop(text):
    decision = detect_refusal(Message(role="assistant", content=text))
    assert decision is not None
    assert {choice["id"] for choice in decision.next_steps} == {"local_static_analysis", "remediation", "ask_scope"}
    assert decision.to_event()["automatic_retry"] is False


@pytest.mark.parametrize("role,text", [
    ("tool", "I cannot fulfill this request."),
    ("user", "I cannot fulfill this request."),
    ("assistant", '> I cannot fulfill this request.\nThe tool quoted this string.'),
    ("assistant", 'Example: "I cannot fulfill this request."'),
    ("assistant", '```text\nI cannot fulfill this request.\n```\nThis is a test fixture.'),
    ("assistant", 'Tool result:\nI cannot fulfill this request.\n\nThat string is evidence.'),
    ("assistant", '<blockquote>I cannot fulfill this request.</blockquote>\nThe document quoted a refusal.'),
    ("assistant", "I cannot fix the parser because the schema is missing."),
    ("assistant", "As an AI assistant, I found a guard in the code."),
])
def test_quoted_tool_text_and_ordinary_capability_errors_are_not_refusals(role, text):
    assert detect_refusal(Message(role=role, content=text)) is None


def test_refusal_preserves_original_and_never_retries_or_executes_attached_tools():
    async def scenario():
        refusal = "I cannot fulfill this request."
        model = Model([ModelResponse(content=refusal, tool_calls=[ToolCall(id="stop", name="read_file")])])
        registry, events = Registry(), []
        agent = Agent(model, registry, event_callback=events.append)
        result = await agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Review source"))
        assert result.status == TaskStatus.BLOCKED and result.summary == refusal
        assert len(model.calls) == 1 and model.calls[0][1]["temperature"] == .1
        assert registry.calls == []
        assert agent._strategy_attempts == 0
        assert any(message.role == "assistant" and message.content == refusal for message in agent.state.messages)
        assert agent.state.messages[-1].tool_call_id == "stop"
        notice = next(event for event in events if event.event_type == "model_refusal_detected")
        assert notice.data["original_retained"] and not notice.data["automatic_retry"]
    asyncio.run(scenario())


def test_refusal_report_keeps_completed_steps_and_baseline_as_unverified_evidence():
    async def scenario():
        model = Model([ModelResponse(tool_calls=[ToolCall(id="read", name="read_file")]), ModelResponse(content="I cannot fulfill this request.")])
        task = AgentTask(role=AgentRole.SECURITY, objective="Review local source")
        task.context.previous_findings = ["Static scan candidate at app.py:4; not verified."]
        agent = Agent(model, Registry())
        result = await agent.execute(task)
        assert result.summary.startswith("I cannot fulfill this request.")
        assert "read_file: succeeded" in result.summary
        assert "app.py:4" in result.summary and "unverified" in result.summary
        assert "Remediation" in result.summary or "remediation" in result.summary
        assert len(model.calls) == 2
    asyncio.run(scenario())


def test_non_safety_incomplete_step_keeps_bounded_recovery():
    async def scenario():
        model = Model([ModelResponse(content="I cannot fix the parser because the schema is missing."), ModelResponse(content="Inspected the schema and verified the fix.")])
        agent = Agent(model, Registry())
        result = await agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Fix parser"))
        assert result.status == TaskStatus.COMPLETED
        assert agent._strategy_attempts == 1 and len(model.calls) == 2
    asyncio.run(scenario())


def test_output_caps_shrink_monotonically_without_increasing_small_limits():
    limits = [sliding_tool_output_budget(8000, 400, depth) for depth in [0, .25, .5, .75, 1]]
    assert limits[0] == (8000, 400)
    assert limits[-1][0] < limits[0][0] and limits[-1][1] < limits[0][1]
    assert all(a[0] >= b[0] and a[1] >= b[1] for a, b in zip(limits, limits[1:]))
    assert sliding_tool_output_budget(50, 2, 1)[0] <= 50
    agent = Agent(Model([]), Registry(), max_tool_output_chars=8000)
    call = ToolCall(id="log", name="run_tests")
    text = "test evidence\n" * 1000
    early = agent._fit_tool_output(call, text)
    agent._last_prompt_tokens = 31000
    late = agent._fit_tool_output(call, text)
    assert len(late) < len(early)
    assert agent.max_tool_output_chars == 8000


def test_success_outputs_are_pruned_before_failed_evidence_and_protocol_is_exact():
    messages = [Message(role="system", content="rules"), Message(role="user", content="task")]
    for identity, content in [("failure", "failing assertion " * 100), ("success", "directory listing " * 100), ("recent", "current")]:
        messages.extend([Message(role="assistant", content="reasoning " + identity, reasoning_content="opaque", tool_calls=[ToolCall(id=identity, name="run_tests")]), Message(role="tool", content=content, tool_call_id=identity)])
    before = sum(len(message.model_dump_json()) for message in messages)
    result, freed = prune_tool_outputs(messages, protect_chars=10, outcomes={"failure": False, "success": True, "recent": True}, target_chars=before - 1000)
    assert freed >= 1000
    assert result[3].content == messages[3].content
    assert len(result[5].content) < len(messages[5].content)
    assert [(m.role, m.tool_call_id) for m in result] == [(m.role, m.tool_call_id) for m in messages]
    assert [m.model_dump_json() for m in result if m.role == "assistant"] == [m.model_dump_json() for m in messages if m.role == "assistant"]


def test_failed_repeat_does_not_supersede_an_earlier_success():
    calls = [ToolCall(id="a", name="read_file", arguments={"path": "a.py"}), ToolCall(id="b", name="read_file", arguments={"path": "a.py"})]
    messages = [Message(role="assistant", content="", tool_calls=[calls[0]]), Message(role="tool", content="original evidence " * 30, tool_call_id="a"), Message(role="assistant", content="", tool_calls=[calls[1]]), Message(role="tool", content="Permission denied", tool_call_id="b")]
    result, _ = prune_tool_outputs(messages, protect_chars=10000, outcomes={"a": True, "b": False})
    assert result == messages


def test_existing_ledger_is_pinned_verbatim_only_when_it_fits(tmp_path):
    from local_coder.tools import create_tool_registry
    ledger = tmp_path / ".local-coder/security/ledger.json"
    ledger.parent.mkdir(parents=True)
    original = '[\n  {"title": "exact source evidence", "line": 42}\n]\n'
    ledger.write_text(original)
    agent = Agent(Model([]), create_tool_registry(str(tmp_path)))
    messages = [Message(role="system", content="rules"), Message(role="user", content="task"), Message(role="assistant", content="older thoughts")]
    pinned = agent._repin(messages)
    notes = [message.content for message in pinned if message.content.startswith(PINNED_PREFIX)]
    assert len(notes) == 1 and notes[0].endswith(original)
    assert "untrusted" in notes[0].lower()
    agent.max_pinned_context_chars = 32
    bounded = agent._repin(messages)
    assert not any(original in message.content for message in bounded)
    assert any("ledger.json" in message.content for message in bounded)


def test_delegated_blocked_signal_propagates_without_parent_recovery():
    from local_coder.orchestrator.blocked import AgentBlocked
    from local_coder.types import AgentResponse

    async def scenario():
        blocked = AgentResponse(task_id="child", status=TaskStatus.BLOCKED, summary="I cannot fulfill this request.")
        class ChildRegistry(Registry):
            def get_schemas_for_role(self, role):
                return fake_schemas("delegate_task")

            async def execute_tool(self, role, name, arguments):
                raise AgentBlocked(blocked)
        model = Model([ModelResponse(tool_calls=[ToolCall(id="delegate", name="delegate_task")])])
        agent = Agent(model, ChildRegistry())
        with pytest.raises(AgentBlocked) as captured:
            await agent.execute(AgentTask(role=AgentRole.SECURITY, objective="Review local source"))
        assert captured.value.response is blocked
        assert len(model.calls) == 1 and agent._strategy_attempts == 0
    asyncio.run(scenario())

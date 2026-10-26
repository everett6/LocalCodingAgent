"""Bounded recovery and archival tested with deterministic local models."""
import asyncio
import json
from pathlib import Path
import pytest
from tests.test_agents import FakeModel, FakeRegistry, LoopAgent
from tests.test_harness_recovery import HarnessAgent, Registry, ScriptedModel, ProcessCrash
from local_coder.core.runtime import AgentRuntime
from local_coder.types import AgentTask, AgentRole, ModelResponse, ToolCall, ToolResult, TaskStatus


def test_surrender_retries_with_alternate_strategy_and_bounded_budget():
    model = FakeModel([ModelResponse(content="I cannot complete this task.")] * 5)
    events = []
    agent = LoopAgent(model, FakeRegistry([]), event_callback=events.append)
    result = asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Fix a local bug")))
    assert result.status == TaskStatus.FAILED
    assert len(model.calls) == 4
    assert len([e for e in events if e.event_type == 'strategy_recovery']) == 3
    assert any(e.event_type == 'strategy_budget_exhausted' for e in events)
    strategies = [m.content for m in model.calls[-1][0] if 'Recovery strategy' in m.content]
    assert len(strategies) == 3
    assert 'different tool' in strategies[1]


@pytest.mark.parametrize('reason,expected_status', [
    ('I cannot complete this task: authorization is missing.', TaskStatus.FAILED),
    ('I cannot help with this harmful request under safety policy.', TaskStatus.BLOCKED),
    ('I cannot continue: user cancelled the task.', TaskStatus.FAILED),
])
def test_protected_stops_do_not_trigger_recovery(reason, expected_status):
    model = FakeModel([ModelResponse(content=reason)])
    agent = LoopAgent(model, FakeRegistry([]))
    result = asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Inspect")))
    assert result.status == expected_status
    assert len(model.calls) == 1


def test_failed_verification_recovers_and_observes_a_passing_test():
    model = FakeModel([ModelResponse(tool_calls=[ToolCall(name='run_tests')]),
                       ModelResponse(content='I cannot fix this.'),
                       ModelResponse(tool_calls=[ToolCall(name='run_tests')]),
                       ModelResponse(content='Verified.')])
    agent = LoopAgent(model, FakeRegistry([ToolResult(success=False, output='assertion failed'),
                                          ToolResult(success=True, output='passed')]))
    result = asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective='Fix')))
    assert result.status == TaskStatus.COMPLETED
    assert agent._strategy_attempts == 1
    assert len(agent._progress_journal) == 2


def test_recovery_checkpoint_preserves_remaining_attempts(tmp_path):
    async def scenario():
        runtime = AgentRuntime(str(tmp_path))
        agent = HarnessAgent(ScriptedModel([ModelResponse(content='I cannot complete the task.'), ProcessCrash()]), Registry(runtime))
        with pytest.raises(ProcessCrash):
            await agent.execute(AgentTask(role=AgentRole.SECURITY, objective='Inspect'))
        path = max(runtime.state_dir.glob('checkpoint_*.json'), key=lambda p: json.loads(p.read_text())['iteration'])
        assert json.loads(path.read_text())['notes']['strategy_attempts'] == 1
        restarted = AgentRuntime(str(tmp_path))
        run = await restarted.recover_from_checkpoint(agent.state.run_id, str(path))
        model = ScriptedModel([ModelResponse(content='I cannot complete the task.')] * 3)
        resumed = HarnessAgent(model, Registry(restarted))
        result = await resumed.resume(run)
        assert result.status == TaskStatus.FAILED
        assert len(model.calls) == 3
        assert resumed._strategy_attempts == 3
    asyncio.run(scenario())


def test_oversized_tool_text_archived_without_extra_model_calls(tmp_path):
    async def scenario():
        runtime = AgentRuntime(str(tmp_path))
        model = ScriptedModel([ModelResponse(tool_calls=[ToolCall(id='read', name='read_file')], reasoning_content='protocol-state'),
                               ModelResponse(content='Reviewed.')])
        agent = HarnessAgent(model, Registry(runtime, [ToolResult(success=True, output='HEAD\n' + 'data ' * 10000 + '\nTAIL')]),
                             context_window_chars=3500, compact_context_chars=2200)
        result = await agent.execute(AgentTask(role=AgentRole.SECURITY, objective='Inspect the input', constraints=['Keep scope local']))
        assert result.status == TaskStatus.COMPLETED
        assert len(model.calls) == 2
        history = model.calls[1]
        assert any(m.reasoning_content == 'protocol-state' for m in history)
        assert 'Keep scope local' in history[1].content
        archive, = (runtime.state_dir / 'context-archives').glob('*.json')
        saved = json.loads(archive.read_text())
        assert len(saved[-1]['content']) > 50000
        assert str(archive) in str(history)
    asyncio.run(scenario())


def test_application_policy_or_safety_code_failure_is_not_a_policy_refusal():
    model=FakeModel([ModelResponse(content='I cannot complete this task because the policy parser crashes.'),ModelResponse(content='Inspected the parser and identified the failing boundary.')])
    agent=LoopAgent(model,FakeRegistry([]))
    result=asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER,objective='Diagnose the application policy parser')))
    assert result.status==TaskStatus.COMPLETED and len(model.calls)==2


def test_too_difficult_response_requires_alternate_strategy_attempt():
    model=FakeModel([ModelResponse(content='This task is too difficult to complete.'),ModelResponse(content='Narrowed the problem and verified the relevant behavior.')])
    agent=LoopAgent(model,FakeRegistry([]))
    result=asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER,objective='Investigate a difficult bug')))
    assert result.status==TaskStatus.COMPLETED and len(model.calls)==2

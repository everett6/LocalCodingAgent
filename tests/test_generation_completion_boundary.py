"""No backend may turn a truncated response into an executable tool batch."""
import asyncio

import pytest

from local_coder.types import AgentRole, AgentTask, ModelResponse, TaskStatus, ToolCall
from local_coder.core.runtime import AgentRuntime
from tests.test_harness_recovery import HarnessAgent, Registry, ScriptedModel


@pytest.mark.parametrize('finish_reason', ['length', 'max_tokens'])
def test_truncated_tool_batch_never_reaches_repair_or_execution(tmp_path, monkeypatch, finish_reason):
    async def scenario():
        registry = Registry(AgentRuntime(str(tmp_path)), tool_names=('write_file',))
        model = ScriptedModel([ModelResponse(finish_reason=finish_reason, tool_calls=[
            ToolCall(name='write_file', arguments={'path': 'fixture.txt', 'content': 'incomplete'}),
        ])])
        agent = HarnessAgent(model, registry)

        def forbidden_repair(*args):
            pytest.fail('Truncated tool arguments must not be repaired into executable calls')

        monkeypatch.setattr(agent, '_repair_tool_calls', forbidden_repair)
        result = await agent.execute(AgentTask(role=AgentRole.SECURITY, objective='fixture only'))
        assert result.status == TaskStatus.FAILED
        assert result.tests_run == []
        assert any('truncated' in issue.lower() for issue in result.issues)
        assert registry.calls == []
        assert result.metrics.tool_calls == 0
        assert agent.state.tool_calls_used == 0
        assert not list(registry.runtime.state_dir.glob('run_*_pending.json'))
        assert len(model.calls) == 1
        assert not (tmp_path / 'fixture.txt').exists()
    asyncio.run(scenario())

"""Investigation resource caps and durable progress must survive specialist setup."""
import asyncio
from types import SimpleNamespace

import pytest

from local_coder.agents.bug_hunter import BugHunterAgent
from local_coder.agents.red_team import RedTeamAgent
from local_coder.redteam import EngagementScope, EngagementStore
from local_coder.tools import create_tool_registry
from local_coder.tools.redteam import bind_engagement, RedTeamWorkspaceTool
from local_coder.types import AgentTask, ModelResponse, TaskStatus, ToolCall


class ReadingModel:
    def __init__(self, role, profile):
        self.config = SimpleNamespace(temperature=0, max_tokens=100, profile=profile, context_length=100000)
        self.role = role
        self.calls = 0

    async def generate(self, messages, **kwargs):
        self.calls += 1
        name = 'redteam_workspace' if self.role == RedTeamAgent.role else 'read_file'
        arguments = {'action': 'read', 'path': 'app.py'} if name == 'redteam_workspace' else {'path': 'app.py'}
        return ModelResponse(tool_calls=[ToolCall(name=name, arguments=arguments)])


@pytest.mark.parametrize('agent_type', [BugHunterAgent, RedTeamAgent])
@pytest.mark.parametrize('limit', ['iterations', 'tool_calls'])
def test_specialist_execution_preserves_lower_host_caps(tmp_path, agent_type, limit):
    async def scenario():
        (tmp_path / 'app.py').write_text('value = 1\n')
        registry = create_tool_registry(str(tmp_path))
        engagement = EngagementStore(str(tmp_path)).create(EngagementScope(name='Review', objective='Read source'))
        bind_engagement(registry, str(tmp_path), engagement['id'])
        model = ReadingModel(agent_type.role, 'qwen38')
        agent = agent_type(model, registry, context_window_chars=12000, compact_context_chars=6000)
        agent.max_iterations = 1 if limit == 'iterations' else 10
        agent.max_tool_calls = 1 if limit == 'tool_calls' else 100
        result = await agent.execute(AgentTask(role=agent_type.role, objective='Read source',
                                              engagement_id=engagement['id']))
        assert result.status != TaskStatus.COMPLETED
        if limit == 'iterations':
            assert model.calls == 1
            assert agent.state.max_iterations == 1
        else:
            assert agent.state.tool_calls_used == 1
            assert agent.state.max_tool_calls == 1
        assert agent.context_window_chars == 12000
        assert agent.compact_context_chars == 6000
    asyncio.run(scenario())


def test_replaying_reads_outside_recent_window_exhausts_progress_budget(tmp_path):
    async def scenario():
        store = EngagementStore(str(tmp_path))
        engagement = store.create(EngagementScope(name='Review', objective='Read source'))
        workspace = RedTeamWorkspaceTool(store, engagement['id'])
        for index in range(21):
            path = f'source_{index}.py'
            (tmp_path / path).write_text(f'value = {index}\n')
            assert (await workspace.execute(action='read', path=path)).success
        registry = create_tool_registry(str(tmp_path))
        bind_engagement(registry, str(tmp_path), engagement['id'])

        class CyclingModel(ReadingModel):
            async def generate(self, messages, **kwargs):
                self.calls += 1
                return ModelResponse(tool_calls=[ToolCall(name='redteam_workspace', arguments={
                    'action': 'read', 'path': f'source_{(self.calls - 1) % 21}.py'})])

        model = CyclingModel(RedTeamAgent.role, 'generic')
        agent = RedTeamAgent(model, registry)
        agent.max_iterations = 40
        result = await agent.execute(AgentTask(role=agent.role, objective='Read source',
                                              engagement_id=engagement['id']))
        assert result.status == TaskStatus.BLOCKED
        assert model.calls <= 9
        assert any('no_progress' in issue for issue in result.issues)
    asyncio.run(scenario())

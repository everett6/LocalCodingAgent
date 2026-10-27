"""Host boundaries for bounded discovery, parallel writes, and useful progress."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from local_coder.tools.base import ToolRegistry
from local_coder.tools.filesystem import EditFileTool
from local_coder.tools.tool_catalog import ToolCatalogTool
from local_coder.types import AgentRole


def test_parallel_scope_rejects_sibling_and_symlink_before_mutation(tmp_path):
    (tmp_path / 'owned').mkdir()
    other = tmp_path / 'other.txt'
    other.write_text('old')
    (tmp_path / 'owned' / 'alias').symlink_to(other)
    registry = ToolRegistry()
    registry.project_root = str(tmp_path)
    registry.register(EditFileTool(str(tmp_path)))

    async def scenario():
        token = registry.task_context.set({'parallel_worker': True, 'write_scope': [str(tmp_path / 'owned')]})
        try:
            for path in ('other.txt', 'owned/alias'):
                result = await registry.execute_tool(AgentRole.CODER, 'edit_file', {
                    'path': path, 'old_string': 'old', 'new_string': 'changed'})
                assert not result.success
                assert 'scope' in result.output.lower()
            assert other.read_text() == 'old'
        finally:
            registry.task_context.reset(token)
    asyncio.run(scenario())


def test_catalog_injects_host_role_and_never_adds_permissions():
    registry = ToolRegistry()
    registry.register(ToolCatalogTool(registry))
    result = asyncio.run(registry.execute_tool(AgentRole.EXPLORER, 'tool_catalog', {'names': ['run_command']}))
    assert result.success
    assert result.data['schemas'] == []
    assert 'run_command' not in result.data['permitted_names']


def test_noop_progress_requires_new_evidence_or_revision():
    from local_coder.agents.progress import ProgressGuard
    guard = ProgressGuard()
    for index in range(4):
        result = guard.observe('read_file', {'path': 'a.py'}, 'unchanged', success=True, revision='one')
    assert result['action'] == 'recover'
    assert guard.observe('read_file', {'path': 'a.py'}, 'changed', success=True, revision='two')['action'] == 'continue'
    for _ in range(20):
        result = guard.observe('read_file', {'path': 'a.py'}, 'changed', success=True, revision='two')
    assert result['action'] == 'stop'
    assert len(guard.snapshot()['observations']) <= 32


def test_model_profile_preference_changes_actual_catalog():
    from local_coder.context.catalog import select_tool_catalog
    schemas = [{'function': {'name': name}} for name in ('read_file', 'edit_file', 'run_tests')]
    result = select_tool_catalog(schemas, role='coder', task='update', char_budget=2000,
                                 preferred_names=['run_tests', 'edit_file', 'read_file'])
    assert [row['function']['name'] for row in result['schemas']] == ['run_tests', 'edit_file', 'read_file']


def test_cancelled_agent_records_diagnostic_outcome(tmp_path):
    from local_coder.agents.base import BaseAgent
    from local_coder.context.telemetry import ContextTelemetry
    from local_coder.types import AgentTask

    async def scenario():
        entered = asyncio.Event()
        class Model:
            config = SimpleNamespace(temperature=0, max_tokens=100)
            async def generate(self, *args, **kwargs):
                entered.set()
                await asyncio.Future()
        class Agent(BaseAgent):
            role = AgentRole.CODER
            system_prompt = 'Inspect local code.'
        registry = ToolRegistry()
        registry.project_root = str(tmp_path)
        task = AgentTask(role=AgentRole.CODER, objective='Inspect cancellation')
        pending = asyncio.create_task(Agent(Model(), registry).execute(task))
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        record = ContextTelemetry(tmp_path).inspect(task_id=task.task_id)['requests'][0]
        assert record['outcome'] == 'cancelled'
        assert record['usage'] is None and record['cost_usd'] is None
    asyncio.run(scenario())


def test_handoff_is_bounded_and_retains_remaining_limits():
    from tests.test_tool_scheduling import Agent, Model
    agent = Agent(Model([]), ToolRegistry())
    agent._files_changed = {'folder/' + str(index) + 'x' * 200 for index in range(100)}
    agent._archive_references = ['archive/' + str(index) + 'x' * 900 for index in range(30)]
    import json
    handoff = agent._handoff()
    assert len(json.dumps(handoff).encode()) <= 4000
    assert handoff['remaining']['tools'] == agent.max_tool_calls
    assert handoff['omitted_details']

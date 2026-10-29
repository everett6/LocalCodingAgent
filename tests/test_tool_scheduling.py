"""Event-controlled scheduling and process recovery tests; no timing guesses."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from local_coder.agents.base import BaseAgent
from local_coder.core.runtime import AgentRuntime
from local_coder.orchestrator.blocked import AgentBlocked
from local_coder.tools.base import Tool, ToolRegistry
from local_coder.tools.filesystem import ReadFileTool
from local_coder.tools.scheduling import ToolEffects, scheduled_calls, tool_effects
from local_coder.types import AgentRole, AgentTask, ModelResponse, TaskStatus, ToolCall, ToolName, ToolResult


class ReadTool(Tool):
    name = ToolName.READ_FILE
    description = 'Read a fixture'
    parameters = {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}
    read_only = True
    mutating = False
    retry_safe = True
    async def execute(self, **kwargs):
        return ToolResult(success=True, output=kwargs['path'])


class WriteTool(Tool):
    name = ToolName.WRITE_FILE
    description = 'Mutate a fixture'
    parameters = {'type': 'object', 'properties': {}}
    async def execute(self, **kwargs):
        return ToolResult(success=True, output='written')


class Model:
    config = SimpleNamespace(temperature=0, max_tokens=100)
    def __init__(self, calls):
        self.responses = iter([ModelResponse(tool_calls=calls), ModelResponse(content='Finished')])
    async def generate(self, messages, **kwargs):
        return next(self.responses)


class Agent(BaseAgent):
    role = AgentRole.CODER
    system_prompt = 'Use the supplied tools.'


def test_effects_default_unsafe_and_exact_known_reads(tmp_path):
    assert tool_effects(WriteTool()) == ToolEffects()
    assert tool_effects(ReadFileTool(str(tmp_path))).concurrent
    assert tool_effects(ReadTool()).concurrent
    class ChangedRead(ReadTool):
        pass
    assert not tool_effects(ChangedRead()).concurrent
    registry = ToolRegistry()
    assert registry.effects_for('invented_tool') == ToolEffects()
    assert registry.effects_for('read_file') == ToolEffects()


def test_adjacent_reads_overlap_and_mutations_are_barriers():
    async def scenario():
        calls = [ToolCall(id=str(index), name=name) for index, name in enumerate(['read', 'read', 'write', 'write', 'read', 'read'])]
        effects = lambda call: ToolEffects(True, False, True) if call.name == 'read' else ToolEffects()
        started = [asyncio.Event() for _ in calls]
        release_reads = asyncio.Event()
        order = []
        async def execute(call):
            index = int(call.id)
            started[index].set()
            order.append(('start', index))
            if index < 2:
                await release_reads.wait()
            elif index == 2:
                assert all(('end', read) in order for read in (0, 1))
                assert not started[3].is_set()
                assert not started[4].is_set()
            elif index == 3:
                assert ('end', 2) in order
            else:
                assert ('end', 3) in order
            order.append(('end', index))
            return index
        async def consume():
            return [(call.id, result) async for call, result in scheduled_calls(calls, execute, effects)]
        pending = asyncio.create_task(consume())
        await started[0].wait()
        await started[1].wait()
        assert not started[2].is_set()
        release_reads.set()
        assert await pending == [(str(index), index) for index in range(6)]
    asyncio.run(scenario())


def test_concurrency_never_exceeds_four():
    async def scenario():
        calls = [ToolCall(id=str(i), name='read') for i in range(9)]
        active = maximum = 0
        first_four = asyncio.Event()
        release = asyncio.Event()
        async def execute(call):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            if active == 4:
                first_four.set()
            await release.wait()
            active -= 1
            return call.id
        async def consume():
            return [result async for _, result in scheduled_calls(calls, execute, lambda _: ToolEffects(True, False, True))]
        task = asyncio.create_task(consume())
        await first_four.wait()
        assert maximum == 4
        release.set()
        assert await task == [str(i) for i in range(9)]
        assert maximum == 4
    asyncio.run(scenario())


def test_failure_cancels_and_awaits_other_reads_before_mutation():
    async def scenario():
        entered, cleaned = asyncio.Event(), asyncio.Event()
        mutations = []
        calls = [ToolCall(name='read', id='a'), ToolCall(name='read', id='b'), ToolCall(name='write', id='c')]
        async def execute(call):
            if call.id == 'a':
                await entered.wait()
                raise RuntimeError('failed read')
            if call.id == 'b':
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            mutations.append(call.id)
        with pytest.raises(RuntimeError, match='failed read'):
            async for _ in scheduled_calls(calls, execute, lambda call: ToolEffects(True, False, True) if call.name == 'read' else ToolEffects()):
                pass
        assert cleaned.is_set()
        assert mutations == []
    asyncio.run(scenario())


def test_real_agent_preserves_context_ids_and_order_for_parallel_reads():
    async def scenario():
        registry = ToolRegistry()
        seen = []
        started = {name: asyncio.Event() for name in ('one', 'two')}
        tool = ReadTool()
        async def execute(path):
            started[path].set()
            await started['two' if path == 'one' else 'one'].wait()
            seen.append(dict(registry.task_context.get()))
            return ToolResult(success=True, output=path)
        tool.execute = execute
        registry.register(tool)
        calls = [ToolCall(id=name, name='read_file', arguments={'path': name}) for name in ('one', 'two')]
        task = AgentTask(role=AgentRole.CODER, objective='Read fixtures')
        agent = Agent(Model(calls), registry)
        response = await agent.execute(task)
        assert response.status == TaskStatus.COMPLETED
        assert all(context['task_id'] == task.task_id for context in seen)
        assert registry.task_context.get() is None
        results = [m for m in agent.state.messages if m.role == 'tool']
        assert [(r.tool_call_id, r.content) for r in results] == [('one', 'one'), ('two', 'two')]
        assert response.metrics.tool_calls == 2
    asyncio.run(scenario())


def test_permissions_and_schema_still_gate_concurrent_tools():
    async def scenario():
        registry = ToolRegistry()
        tool = ReadTool()
        executed = []
        async def execute(**kwargs):
            executed.append(kwargs)
            return ToolResult(success=True)
        tool.execute = execute
        registry.register(tool)
        registry._permissions[AgentRole.CODER].discard(ToolName.READ_FILE)
        agent = Agent(Model([ToolCall(name='read_file', arguments={'path': 'one'})]), registry)
        await agent.execute(AgentTask(role=AgentRole.CODER, objective='Read'))
        assert not executed
        registry._permissions[AgentRole.CODER].add(ToolName.READ_FILE)
        agent = Agent(Model([ToolCall(name='read_file', arguments={'unknown': 'one'})]), registry)
        await agent.execute(AgentTask(role=AgentRole.CODER, objective='Read'))
        assert not executed
    asyncio.run(scenario())


def test_unknown_mutation_blocks_and_journal_prevents_replay(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        runtime = registry.runtime = AgentRuntime(str(tmp_path))
        tool = WriteTool()
        attempts = []
        async def execute(**kwargs):
            attempts.append('side effect')
            raise RuntimeError('lost response after write')
        tool.execute = execute
        registry.register(tool)
        registry.register(ReadTool())
        agent = Agent(Model([ToolCall(id='read', name='read_file', arguments={'path': 'fixture'}), ToolCall(id='mutation', name='write_file')]), registry)
        with pytest.raises(AgentBlocked, match='blocked'):
            await agent.execute(AgentTask(role=AgentRole.CODER, objective='Write fixture'))
        assert attempts == ['side effect']
        pending = json.loads((runtime.state_dir / f'run_{agent.state.run_id}_pending.json').read_text())
        assert pending['tools'][0]['status'] == 'completed'
        assert pending['tools'][1]['status'] == 'unknown'
        assert pending['tools'][1]['mutation_unknown'] is True
        assert 'arguments' not in pending['tools'][1]
        path = runtime.state_dir / f'checkpoint_{agent.state.run_id}_0.json'
        restarted = AgentRuntime(str(tmp_path))
        run = await restarted.recover_from_checkpoint(agent.state.run_id, str(path))
        assert run is not None
        with pytest.raises(ValueError, match='inspect its effects'):
            await Agent(Model([]), registry).resume(run)
        assert attempts == ['side effect']
    asyncio.run(scenario())


def test_cancelled_mutation_keeps_unknown_started_marker(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        registry.runtime = AgentRuntime(str(tmp_path))
        tool = WriteTool()
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def execute(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        tool.execute = execute
        registry.register(tool)
        agent = Agent(Model([ToolCall(id='writing', name='write_file')]), registry)
        pending = asyncio.create_task(agent.execute(AgentTask(role=AgentRole.CODER, objective='Write')))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert stopped.is_set()
        journal = json.loads((registry.runtime.state_dir / f'run_{agent.state.run_id}_pending.json').read_text())
        assert journal['tools'][0]['status'] == 'started'
        assert journal['tools'][0]['mutation_unknown'] is True
    asyncio.run(scenario())


def test_duplicate_ids_execute_nothing():
    async def scenario():
        registry = ToolRegistry()
        tool = ReadTool()
        executed = []
        async def execute(**kwargs):
            executed.append(kwargs)
            return ToolResult(success=True)
        tool.execute = execute
        registry.register(tool)
        calls = [ToolCall(id='same', name='read_file', arguments={'path': path}) for path in ('one', 'two')]
        response = await Agent(Model(calls), registry).execute(AgentTask(role=AgentRole.CODER, objective='Read'))
        assert response.status == TaskStatus.FAILED
        assert not executed
    asyncio.run(scenario())


@pytest.mark.parametrize('completed_first', [False, True])
def test_tool_quota_preserves_pending_journal_and_last_complete_checkpoint(tmp_path, completed_first):
    from local_coder.providers.quota import ProviderQuotaExhausted

    async def scenario():
        registry = ToolRegistry()
        runtime = registry.runtime = AgentRuntime(str(tmp_path))
        tool = WriteTool()
        effects = []
        quota = ProviderQuotaExhausted({'blocked': True})
        async def execute(**kwargs):
            effects.append('mutation before quota')
            raise quota
        tool.execute = execute
        registry.register(tool)
        registry.register(ReadTool())
        calls = ([ToolCall(id='read', name='read_file', arguments={'path': 'fixture'})] if completed_first else [])
        calls.append(ToolCall(id='write', name='write_file'))
        agent = Agent(Model(calls), registry)
        with pytest.raises(ProviderQuotaExhausted) as caught:
            await agent.execute(AgentTask(role=AgentRole.CODER, objective='Write fixture'))
        assert caught.value is quota
        assert effects == ['mutation before quota']
        assert quota.data['recovery_required'] is True
        assert quota.data['automatic_resume'] is False
        checkpoints = list(runtime.state_dir.glob('checkpoint_*.json'))
        assert len(checkpoints) == 1
        checkpoint = checkpoints[0]
        assert checkpoint.name.endswith('_0.json')
        assert quota.data['checkpoint'] == str(checkpoint)
        saved = json.loads(checkpoint.read_text())
        assert saved['notes']['boundary_complete'] is True
        assert not any(message['role'] == 'tool' for message in saved['state']['messages'])
        journal_path = runtime.state_dir / f'run_{agent.state.run_id}_pending.json'
        pending = json.loads(journal_path.read_text())
        assert pending['iteration'] == 1
        assert pending['tools'][-1]['status'] == 'unknown'
        assert pending['tools'][-1]['mutation_unknown'] is True
        if completed_first:
            assert pending['tools'][0]['status'] == 'completed'
        restarted = AgentRuntime(str(tmp_path))
        recovered = await restarted.recover_from_checkpoint(agent.state.run_id, str(checkpoint))
        assert recovered is not None
        with pytest.raises(ValueError, match='inspect its effects'):
            await recovered.validate_pending_batch()
        assert journal_path.exists()
        with pytest.raises(ValueError, match='inspect its effects'):
            await Agent(Model([]), registry).resume(recovered)
        assert effects == ['mutation before quota']
    asyncio.run(scenario())


def test_incomplete_batch_cannot_be_marked_as_checkpoint_boundary():
    async def scenario():
        agent = Agent(Model([]), ToolRegistry())
        agent._tool_batch_active = True
        with pytest.raises(ValueError, match='incomplete tool batch'):
            await agent._checkpoint_boundary()
    asyncio.run(scenario())


def test_registry_distinguishes_exception_after_mutation_from_preexecution_rejection():
    async def scenario():
        registry = ToolRegistry()
        writes = []
        tool = WriteTool()
        async def execute(**kwargs):
            writes.append('partial write')
            raise RuntimeError('write response lost')
        tool.execute = execute
        registry.register(tool)
        result = await registry.execute_tool(AgentRole.CODER, 'write_file', {})
        assert writes == ['partial write']
        assert result.data['execution_outcome'] == 'unknown'
        assert result.data['mutation_completion_unknown'] is True
        token = registry.task_context.set({'depth': 1})
        try:
            rejected = await registry.execute_tool(AgentRole.CODER, 'write_file', {})
        finally:
            registry.task_context.reset(token)
        assert rejected.data['execution_outcome'] == 'not_started'
        assert rejected.data['mutation_completion_unknown'] is False
        assert writes == ['partial write']
    asyncio.run(scenario())


def test_known_completed_failed_test_is_feedback_and_checkpointed(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        runtime = registry.runtime = AgentRuntime(str(tmp_path))
        class FailedTest(Tool):
            name = ToolName.RUN_TESTS
            description = 'Known completed failing test'
            parameters = {'type': 'object', 'properties': {}}
            async def execute(self):
                return ToolResult(success=False, output='One assertion failed')
        registry.register(FailedTest())
        agent = Agent(Model([ToolCall(id='test', name='run_tests')]), registry)
        response = await agent.execute(AgentTask(role=AgentRole.CODER, objective='Run tests'))
        assert response.status == TaskStatus.FAILED
        assert not response.tests_passed
        assert response.metrics.tool_calls == 1
        assert not list(runtime.state_dir.glob('*pending.json'))
        saved = json.loads((runtime.state_dir / f'checkpoint_{agent.state.run_id}_1.json').read_text())
        assert saved['notes']['boundary_complete'] is True
        assert saved['state']['messages'][-1]['content'] == 'One assertion failed'
    asyncio.run(scenario())

"""The recovered features run through host task identity and verification gates."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from local_coder.orchestrator.coordinator import Coordinator
from local_coder.orchestrator.mailbox import PlanMailbox
from local_coder.tools import create_tool_registry
from local_coder.tools.visual import VisualVerificationTool
from local_coder.types import AgentResponse, AgentRole, AgentTask, ProjectConfig, SafetyTier, TaskPlan, TaskStatus, ToolResult, VerificationConfig


def done(task_id='project-tests'):
    return AgentResponse(task_id=task_id, status=TaskStatus.COMPLETED, summary='Observed project tests passed.',
                         tests_run=['project tests'], tests_passed=True)


def test_parallel_tasks_exchange_messages_with_host_bound_identity(tmp_path):
    async def scenario():
        coordinator = Coordinator(ProjectConfig(), str(tmp_path))
        ready = asyncio.Event()
        received = []
        brokers = []
        async def attempt(task, escalate):
            registry = coordinator.tool_registry
            brokers.append(registry.team_mailbox)
            if task.task_id == 'producer':
                result = await registry.execute_tool(task.role, 'team_message', {
                    'action': 'send', 'recipient': 'consumer', 'kind': 'evidence', 'content': 'Fixture parser returns a value.'})
                assert result.success
                ready.set()
            else:
                await asyncio.wait_for(ready.wait(), 1)
                result = await registry.execute_tool(task.role, 'team_message', {'action': 'read'})
                assert result.success
                received.extend(result.data['messages'])
            return done(task.task_id)
        coordinator._attempt_dag_task = attempt
        try:
            plan = TaskPlan(objective="Inspect fixture", tasks=[AgentTask(task_id=name, role=AgentRole.EXPLORER, objective='Inspect fixture')
                                   for name in ('producer', 'consumer')])
            response = await coordinator._execute_plan(plan)
            assert response.status == TaskStatus.COMPLETED
            assert [row['sender'] for row in received] == ['producer']
            assert received[0]['untrusted'] is True
            assert coordinator.tool_registry.team_mailbox is None
            assert all(broker.describe()['closed'] for broker in brokers)
            assert coordinator.tool_registry.task_context.get() is None
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_message_gateway_rejects_forged_identity_but_allows_parallel_channel(tmp_path):
    async def scenario():
        registry = create_tool_registry(str(tmp_path))
        registry.team_mailbox = PlanMailbox(['one', 'two'])
        forged = await registry.execute_tool(AgentRole.CODER, 'team_message', {
            'action': 'read', '_caller_task_id': 'two', '_caller_run_id': registry.team_mailbox.run_id})
        assert not forged.success
        token = registry.task_context.set({'task_id': 'one', 'team_run_id': registry.team_mailbox.run_id,
                                          'parallel_worker': True, 'write_scope': []})
        try:
            result = await registry.execute_tool(AgentRole.CODER, 'team_message', {
                'action': 'send', 'recipient': 'two', 'kind': 'question', 'content': 'Is the fixture ready?'})
            assert result.success
            assert len(registry.team_mailbox.drain('two')) == 1
        finally:
            registry.task_context.reset(token)
            registry.team_mailbox.close()
            await registry.close()
    asyncio.run(scenario())


def test_plan_failure_closes_channels_and_duplicate_ids_never_execute(tmp_path):
    async def scenario():
        coordinator = Coordinator(ProjectConfig(), str(tmp_path))
        brokers = []
        async def broken(task, escalate):
            brokers.append(coordinator.tool_registry.team_mailbox)
            raise RuntimeError('inert fixture failure')
        coordinator._attempt_dag_task = broken
        task = AgentTask(task_id='one', role=AgentRole.EXPLORER, objective='Inspect fixture')
        try:
            with pytest.raises(RuntimeError, match='inert fixture failure'):
                await coordinator._execute_plan(TaskPlan(objective="Inspect fixture", tasks=[task]))
            assert brokers[0].describe()['closed']
            assert coordinator.tool_registry.team_mailbox is None
            response = await coordinator._execute_plan(TaskPlan(objective="Inspect fixture", tasks=[task, task]))
            assert response.status == TaskStatus.FAILED
            assert len(brokers) == 1
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize('status,navigation,expected', [
    ('verified', True, TaskStatus.COMPLETED), ('failed', True, TaskStatus.FAILED),
    ('unavailable', False, TaskStatus.BLOCKED), ('captured', True, TaskStatus.BLOCKED),
])
def test_post_test_visual_hook_requires_actual_verified_assertions(status, navigation, expected):
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.config = ProjectConfig(verification=VerificationConfig(
        visual_url='http://127.0.0.1:8080/', visual_assertions=[{'selector': 'main', 'visible': True}]))
    coordinator._run_project_tests = AsyncMock(return_value=done())
    coordinator._emit = lambda *args, **kwargs: None
    coordinator.tool_registry = SimpleNamespace(execute_tool=AsyncMock(return_value=ToolResult(
        success=status in {'verified', 'captured'}, output='Bounded fixture observation.',
        data={'visual_verification': {'status': status, 'navigation_started': navigation}})))
    response = asyncio.run(coordinator._run_tests())
    assert response.status == expected
    assert response.tests_passed is (expected == TaskStatus.COMPLETED)
    assert response.tests_run == ['project tests'] + (['local UI assertions'] if navigation else [])
    assert coordinator.tool_registry.execute_tool.call_args.args[1] == 'verify_ui'


def test_visual_hook_is_opt_in_and_configuration_rejects_remote_or_empty_checks():
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.config = ProjectConfig()
    coordinator._run_project_tests = AsyncMock(return_value=done())
    coordinator.tool_registry = SimpleNamespace(execute_tool=AsyncMock())
    assert asyncio.run(coordinator._run_tests()).tests_passed
    coordinator.tool_registry.execute_tool.assert_not_called()
    for values in ({'visual_url': 'https://example.invalid', 'visual_assertions': [{'selector': 'main'}]},
                   {'visual_url': 'http://127.0.0.1:8080/'},
                   {'visual_assertions': [{'selector': 'main'}]}):
        with pytest.raises(ValueError):
            VerificationConfig(**values)


def test_visual_tool_respects_read_only_tier_before_approval(tmp_path):
    verifier = SimpleNamespace(verify=AsyncMock())
    approval = AsyncMock(return_value=True)
    tool = VisualVerificationTool(str(tmp_path), approval, verifier=verifier,
                                  safety_harness=SimpleNamespace(tier=SafetyTier.TIER_1_PARANOIA))
    result = asyncio.run(tool.execute('http://127.0.0.1:8080/'))
    assert not result.success and result.data['execution_outcome'] == 'not_started'
    approval.assert_not_called()
    verifier.verify.assert_not_called()

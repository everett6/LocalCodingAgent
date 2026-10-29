"""Explicit refusal is terminal, never a reason to retry on another model."""
import asyncio
from types import SimpleNamespace

import pytest

from local_coder.orchestrator.blocked import AgentBlocked
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.scheduler.dag import TaskDAG
from local_coder.types import AgentMode, AgentResponse, AgentRole, AgentTask, ModelBackend, ModelConfig, ProjectConfig, TaskPlan, TaskStatus


def blocked(task_id='fixture'):
    return AgentResponse(task_id=task_id, status=TaskStatus.BLOCKED, summary='This request exceeds the authorization boundary.', tests_passed=False)


def completed(task_id='fixture'):
    return AgentResponse(task_id=task_id, status=TaskStatus.COMPLETED, summary='Fixture checked.', tests_passed=True)


def setup(tmp_path):
    coordinator = Coordinator(ProjectConfig(project_root=str(tmp_path)), str(tmp_path))
    async def get_model(*args, **kwargs):
        return SimpleNamespace()
    coordinator.model_manager.get_model = get_model
    return coordinator


@pytest.mark.parametrize('method', ['explore', 'plan', 'review', 'execute_simple', 'run_tests', 'fix_failures'])
def test_each_agent_phase_propagates_explicit_block_instead_of_fallback(tmp_path, monkeypatch, method):
    async def scenario():
        coordinator = setup(tmp_path)
        async def execute(task):
            return blocked(task.task_id)
        monkeypatch.setattr('local_coder.orchestrator.coordinator.create_agent', lambda *args, **kwargs: SimpleNamespace(execute=execute))
        monkeypatch.setattr('local_coder.orchestrator.coordinator.detect_framework', lambda root: None)
        calls = {
            'explore': lambda: coordinator._explore('Inspect'),
            'plan': lambda: coordinator._plan('Inspect', 'evidence'),
            'review': lambda: coordinator._review(completed()),
            'execute_simple': lambda: coordinator._execute_simple('Inspect', 'evidence'),
            'run_tests': coordinator._run_tests,
            'fix_failures': lambda: coordinator._fix_failures(AgentResponse(task_id='failed', status=TaskStatus.FAILED, summary='Tests failed', tests_passed=False)),
        }
        try:
            with pytest.raises(AgentBlocked) as caught:
                await calls[method]()
            assert caught.value.response.status == TaskStatus.BLOCKED
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_blocked_coder_stops_before_review_test_fix_or_final_report(tmp_path):
    async def scenario():
        coordinator = setup(tmp_path)
        async def explore(request):
            return 'Fixture'
        async def plan(request, exploration):
            return TaskPlan(objective=request, tasks=[AgentTask(role=AgentRole.CODER, objective=request)])
        async def execute(*args, **kwargs):
            return blocked()
        async def forbidden(*args, **kwargs):
            raise AssertionError('Blocked work must not continue')
        coordinator._explore, coordinator._plan, coordinator._execute_simple = explore, plan, execute
        coordinator._review = coordinator._run_tests = coordinator._fix_failures = coordinator._generate_report = forbidden
        try:
            with pytest.raises(AgentBlocked):
                await coordinator.run('Inspect')
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_dag_blocked_node_never_escalates_or_records_completion(tmp_path):
    async def scenario():
        coordinator = setup(tmp_path)
        events, attempts = [], []
        coordinator.on_event(events.append)
        async def attempt(task, escalate):
            attempts.append(escalate)
            return blocked(task.task_id)
        coordinator._attempt_dag_task = attempt
        coordinator._can_escalate = lambda *args: True
        task = AgentTask(task_id='blocked-task', role=AgentRole.CODER, objective='Inspect')
        dag = TaskDAG()
        dag.add_task(task)
        try:
            with pytest.raises(AgentBlocked):
                await coordinator._run_dag_task(dag, task, asyncio.Semaphore(1))
            assert attempts == [False]
            assert dag.get_node(task.task_id).status == TaskStatus.FAILED
            assert dag.get_node(task.task_id).result.status == TaskStatus.BLOCKED
            assert any(event.event_type == 'task_status' and event.data['status'] == 'blocked' for event in events)
            assert not any(event.event_type == 'escalate' for event in events)
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_blocked_parallel_task_cancels_and_awaits_siblings(tmp_path):
    async def scenario():
        coordinator = setup(tmp_path)
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def run_task(dag, task, semaphore, checkpoint):
            if task.task_id == 'one':
                await started.wait()
                raise AgentBlocked(blocked('one'))
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        coordinator._run_dag_task = run_task
        plan = TaskPlan(objective='Fixture', tasks=[AgentTask(task_id='one', role=AgentRole.EXPLORER, objective='One'), AgentTask(task_id='two', role=AgentRole.EXPLORER, objective='Two')])
        try:
            with pytest.raises(AgentBlocked):
                await coordinator._execute_plan(plan)
            assert cancelled.is_set()
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_saved_blocked_phase_is_never_promoted_to_completed(tmp_path):
    class Checkpoint:
        def get(self, key):
            return blocked().model_dump(mode='json')
        def put(self, key, value):
            raise AssertionError('Do not save blocked output as complete')
    async def scenario():
        coordinator = setup(tmp_path)
        try:
            with pytest.raises(AgentBlocked):
                await coordinator._resumable(Checkpoint(), 'execute', lambda: None, AgentResponse)
            with pytest.raises(AgentBlocked):
                await coordinator._execute_plan(TaskPlan(objective='Fixture', tasks=[AgentTask(role=AgentRole.CODER, objective='One')]), Checkpoint())
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_attempt_exception_handler_never_turns_agent_blocked_into_ordinary_failure(tmp_path, monkeypatch):
    async def scenario():
        coordinator = setup(tmp_path)
        async def execute(task):
            raise AgentBlocked(blocked(task.task_id))
        monkeypatch.setattr('local_coder.orchestrator.coordinator.create_agent', lambda *args, **kwargs: SimpleNamespace(execute=execute))
        try:
            with pytest.raises(AgentBlocked):
                await coordinator._attempt_dag_task(AgentTask(role=AgentRole.CODER, objective='Fixture'), False)
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', [AgentMode.CYBERSECURITY, AgentMode.BUG_HUNTING])
def test_investigation_blocked_status_is_propagated(tmp_path, monkeypatch, mode):
    async def scenario():
        coordinator = setup(tmp_path)
        coordinator.config.mode = mode
        async def execute(task):
            return blocked(task.task_id)
        monkeypatch.setattr('local_coder.orchestrator.coordinator.create_agent', lambda *args, **kwargs: SimpleNamespace(execute=execute))
        try:
            with pytest.raises(AgentBlocked):
                await coordinator.run('Inspect')
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_fix_of_already_blocked_input_never_selects_an_escalation_model(tmp_path):
    async def scenario():
        coordinator = setup(tmp_path)
        async def forbidden(*args, **kwargs):
            raise AssertionError('No model selection after blocked result')
        coordinator._model_for = forbidden
        try:
            with pytest.raises(AgentBlocked):
                await coordinator._fix_failures(blocked(), escalate=True)
            with pytest.raises(AgentBlocked):
                await coordinator._review(blocked())
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


def test_blocked_bug_lead_stops_before_investigation_and_restores_permissions(tmp_path, monkeypatch):
    async def scenario():
        coordinator = setup(tmp_path)
        coordinator.config.mode = AgentMode.BUG_HUNTING
        coordinator.config.models['bug-lead'] = ModelConfig(
            name='bug-lead', backend=ModelBackend.OPENAI_COMPAT, model_id='fixture')
        original = {role: set(tools) for role, tools in coordinator.tool_registry._permissions.items()}
        executed = []

        def create_agent(role, *args, **kwargs):
            async def execute(task):
                executed.append(role)
                return blocked(task.task_id)
            return SimpleNamespace(execute=execute)

        monkeypatch.setattr('local_coder.orchestrator.coordinator.create_agent', create_agent)
        try:
            with pytest.raises(AgentBlocked):
                await coordinator.run('Inspect')
            assert executed == [AgentRole.PLANNER]
            assert coordinator.tool_registry._permissions == original
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize('method', ['review_security_finding', 'prepare_security_regression'])
def test_security_evidence_workflows_preserve_terminal_block_and_permissions(tmp_path, monkeypatch, method):
    from tests.test_security_evidence import setup as setup_evidence

    async def scenario():
        store, identity = setup_evidence(tmp_path)
        coordinator = setup(tmp_path)
        original = {role: set(tools) for role, tools in coordinator.tool_registry._permissions.items()}

        async def execute(task):
            return blocked(task.task_id)

        monkeypatch.setattr('local_coder.orchestrator.coordinator.create_agent',
                            lambda *args, **kwargs: SimpleNamespace(execute=execute))
        try:
            with pytest.raises(AgentBlocked):
                await getattr(coordinator, method)(identity)
            assert coordinator.tool_registry._permissions == original
            assert store.get(identity)['state'] == 'candidate'
        finally:
            await coordinator.model_manager.close_all()
    asyncio.run(scenario())

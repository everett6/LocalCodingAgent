"""Local regressions for truthful, recoverable autonomous execution."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from local_coder.agents.base import BaseAgent
from local_coder.core.runtime import AgentRuntime
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.tools.base import ToolRegistry
from local_coder.types import AgentRole, AgentTask, ModelConfig, ModelResponse, ProjectConfig, TaskStatus


class Model:
    config = ModelConfig(name='fixture', model_id='fixture', max_tokens=512)

    def __init__(self, content='Completed the requested local inspection.'):
        self.content = content

    async def generate(self, messages, **kwargs):
        return ModelResponse(content=self.content)


class Agent(BaseAgent):
    role = AgentRole.CODER
    system_prompt = 'Inspect this local fixture.'


@pytest.mark.parametrize('component', ['memory', 'evolution', 'memory_callback'])
def test_optional_tracking_failure_does_not_discard_completed_run(tmp_path, component):
    registry = ToolRegistry()
    registry.runtime = AgentRuntime(str(tmp_path))
    warnings = []

    def fail(*args, **kwargs):
        raise OSError('Fixture tracking storage unavailable')

    if component.startswith('memory'):
        def ingest(kind, *args, **kwargs):
            if kind == 'decision':
                fail()
        registry.context_memory = SimpleNamespace(ingest=ingest, task_state=lambda *a, **k: None)
    else:
        registry.evolution = SimpleNamespace(active_overlay=lambda: {}, active_revisions=lambda: [{'revision_id': 'fixture'}], record_outcome=fail)
    def observe(event):
        warnings.append(event)
        if component == 'memory_callback' and event.event_type == 'tracking_warning':
            raise RuntimeError('Fixture UI subscriber unavailable')
    agent = Agent(Model(), registry, event_callback=observe)

    async def scenario():
        response = await agent.execute(AgentTask(role=AgentRole.CODER, objective='Inspect the fixture'))
        assert response.status == TaskStatus.COMPLETED
        assert await registry.runtime.list_runs() == []
    asyncio.run(scenario())
    final = list(registry.runtime.state_dir.glob('run_*_final.json'))
    assert len(final) == 1
    assert json.loads(final[0].read_text())['result']['status'] == 'completed'
    assert any(event.event_type == 'tracking_warning' for event in warnings)


@pytest.mark.parametrize('left,right', [('src', 'src/app.py'), ('src/app.py', 'src/./app.py'), ('src/app.py', 'src/../src/app.py')])
def test_parallel_writers_cannot_overlap_through_aliases_or_directories(tmp_path, left, right):
    coordinator = object.__new__(Coordinator)
    coordinator.project_root = str(tmp_path)
    coordinator.config = ProjectConfig(agentic={'max_parallel_agents': 4})
    tasks = [AgentTask(role=AgentRole.CODER, objective='Change first', files=[left]),
             AgentTask(role=AgentRole.BUG_HUNTER, objective='Fix second', files=[right])]
    assert coordinator._parallelism_for_wave(tasks) == 1


def test_disjoint_writer_scopes_still_allow_configured_parallelism(tmp_path):
    coordinator = object.__new__(Coordinator)
    coordinator.project_root = str(tmp_path)
    coordinator.config = ProjectConfig(agentic={'max_parallel_agents': 4})
    tasks = [AgentTask(role=AgentRole.CODER, objective='Change first', files=['a.py']),
             AgentTask(role=AgentRole.BUG_HUNTER, objective='Fix second', files=['b.py'])]
    assert coordinator._parallelism_for_wave(tasks) == 4


def test_model_local_plan_ids_cannot_reuse_another_run_goals(tmp_path):
    coordinator = Coordinator(ProjectConfig(), str(tmp_path))
    plan_text = json.dumps({'tasks': [
        {'task_id': '1', 'objective': 'Implement', 'role': 'coder'},
        {'task_id': '2', 'objective': 'Verify', 'role': 'tester', 'depends_on': ['1']},
    ]})
    coordinator.model_manager.get_model = lambda role: asyncio.sleep(0, result=Model(plan_text))
    first = asyncio.run(coordinator._plan('First objective', 'Fixture evidence'))
    second = asyncio.run(coordinator._plan('Second objective', 'Fixture evidence'))
    assert {t.task_id for t in first.tasks}.isdisjoint(t.task_id for t in second.tasks)
    assert first.tasks[1].depends_on == [first.tasks[0].task_id]

"""Cross-feature contracts that textual merging cannot verify."""
import pytest
from local_coder.agents.tool_repair import parse_arguments
from local_coder.core.runtime import AgentRun
from local_coder.types import AgentRole, AgentState, AgentTask


def test_resume_accounts_for_summary_model_calls_without_resetting_iterations():
    task = AgentTask(role=AgentRole.CODER, objective='Inspect')
    state = AgentState(task_id=task.task_id, objective=task.objective, iteration=1)
    run = AgentRun('test-run', task, state, 0, 5)
    run.notes = {'boundary_complete': True, 'summary_calls': 1}
    run.metrics.model_calls = 2
    run.validate_resume()
    run.notes['summary_calls'] = 0
    with pytest.raises(ValueError, match='metrics disagree'):
        run.validate_resume()


@pytest.mark.parametrize('counter', [-1, True, '1', 3])
def test_invalid_summary_counters_do_not_bypass_resume_validation(counter):
    task = AgentTask(role=AgentRole.CODER, objective='Inspect')
    state = AgentState(task_id=task.task_id, objective=task.objective, iteration=1)
    run = AgentRun('test-run', task, state, 0, 5)
    run.metrics.model_calls = 2
    run.notes = {'boundary_complete': True, 'summary_calls': counter}
    with pytest.raises(ValueError, match='summary counter'):
        run.validate_resume()


def test_partial_empty_tool_arguments_are_not_repaired_into_an_executable_call():
    assert parse_arguments('{') is None
    assert parse_arguments('[') is None
    assert parse_arguments('{}') == {}
    assert parse_arguments('{"path":"app.py"') == {'path': 'app.py'}


def test_persistent_remote_run_closes_models_in_the_same_event_loop(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from local_coder.remote import RemoteControlServer
    loops = []
    class Coordinator:
        def __init__(self):
            async def close_all():
                loops.append(asyncio.get_running_loop())
            self.model_manager = SimpleNamespace(close_all=close_all)
        def on_event(self, handler):
            pass
        async def run(self, request, **kwargs):
            loops.append(asyncio.get_running_loop())
            return 'Completed'
    server = RemoteControlServer(str(tmp_path))
    monkeypatch.setattr(server, '_coordinator', Coordinator)
    status, result = server.handle('POST', '/run', {'request': 'Inspect', 'session_id': 'loop-test'})
    assert status == 200 and result['status'] == 'completed'
    assert len(loops) == 2 and loops[0] is loops[1]

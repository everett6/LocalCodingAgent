"""Partial patch results stay uncertain; all subprocesses are local fakes."""
import asyncio
from types import SimpleNamespace

import pytest

from local_coder.agents.base import BaseAgent
from local_coder.orchestrator.blocked import AgentBlocked
from local_coder.tools.base import ToolRegistry
from local_coder.tools.filesystem import ApplyPatchTool
from local_coder.types import AgentRole, AgentTask, ModelResponse, ToolCall

PATCH = '--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-old\n+new\n'


def fake_process(monkeypatch, path, *, returncode=1, error=None):
    calls = []

    class Process:
        async def communicate(self, data):
            assert data == PATCH.encode()
            path.write_text('new\n')
            if error:
                raise error
            return b'first hunk applied; second hunk failed', b''

    async def start(*args, **kwargs):
        calls.append(args)
        process = Process()
        process.returncode = returncode
        return process

    monkeypatch.setattr('local_coder.tools.filesystem.asyncio.create_subprocess_exec', start)
    return calls


@pytest.mark.parametrize('error', [None, OSError('lost process response')])
def test_partial_patch_exit_or_exception_is_unknown(tmp_path, monkeypatch, error):
    target = tmp_path / 'app.txt'
    target.write_text('old\n')
    calls = fake_process(monkeypatch, target, error=error)
    result = asyncio.run(ApplyPatchTool(str(tmp_path)).execute(PATCH))
    assert target.read_text() == 'new\n' and len(calls) == 1
    assert not result.success and result.files_changed == []
    assert result.data == {'execution_outcome': 'unknown', 'potential_targets': ['app.txt']}


def test_successful_patch_retains_known_completion(tmp_path, monkeypatch):
    target = tmp_path / 'app.txt'
    target.write_text('old\n')
    fake_process(monkeypatch, target, returncode=0)
    result = asyncio.run(ApplyPatchTool(str(tmp_path)).execute(PATCH))
    assert result.success and result.files_changed == ['app.txt']
    assert result.data['execution_outcome'] == 'completed'


def test_malformed_patch_does_not_start_process_or_claim_unknown(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('Malformed patches must not start subprocesses')

    monkeypatch.setattr('local_coder.tools.filesystem.asyncio.create_subprocess_exec', forbidden)
    result = asyncio.run(ApplyPatchTool(str(tmp_path)).execute('not a patch'))
    assert not result.success and result.files_changed == []
    assert result.data == {'execution_outcome': 'not_started', 'potential_targets': []}


def test_process_start_error_is_not_started(tmp_path, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise FileNotFoundError('patch unavailable')

    monkeypatch.setattr('local_coder.tools.filesystem.asyncio.create_subprocess_exec', unavailable)
    result = asyncio.run(ApplyPatchTool(str(tmp_path)).execute(PATCH))
    assert result.data['execution_outcome'] == 'not_started'


class Coder(BaseAgent):
    role = AgentRole.CODER
    system_prompt = 'Use the supplied fixture tools.'


class Model:
    config = SimpleNamespace(temperature=0, max_tokens=100)

    def __init__(self, patch):
        self.responses = iter([
            ModelResponse(tool_calls=[ToolCall(id='first', name='apply_patch', arguments={'patch': patch}),
                                      ToolCall(id='duplicate', name='apply_patch', arguments={'patch': patch})]),
            ModelResponse(content='Finished'),
        ])

    async def generate(self, messages, **kwargs):
        return next(self.responses)


def test_scheduler_blocks_duplicate_after_real_patch_partial_failure(tmp_path, monkeypatch):
    target = tmp_path / 'app.txt'
    target.write_text('old\n')
    calls = fake_process(monkeypatch, target)
    registry = ToolRegistry()
    registry.project_root = str(tmp_path)
    registry.register(ApplyPatchTool(str(tmp_path)))
    agent = Coder(Model(PATCH), registry)
    with pytest.raises(AgentBlocked):
        asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective='Patch fixture')))
    assert target.read_text() == 'new\n'
    assert len(calls) == 1


def test_registry_rejects_malformed_patch_without_false_recovery_block(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('Malformed patches must not start subprocesses')

    monkeypatch.setattr('local_coder.tools.filesystem.asyncio.create_subprocess_exec', forbidden)
    registry = ToolRegistry()
    registry.project_root = str(tmp_path)
    registry.register(ApplyPatchTool(str(tmp_path)))
    result = asyncio.run(registry.execute_tool(AgentRole.CODER, 'apply_patch', {'patch': 'not a patch'}))
    assert not result.success and result.data['execution_outcome'] == 'not_started'
    assert not result.data['mutation_completion_unknown']

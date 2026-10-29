"""Checkpoint digests refer to observed source bytes rather than later disk reads."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from local_coder.agents.base import BaseAgent
from local_coder.core.runtime import AgentRuntime, validate_workspace_file_fingerprints
from local_coder.tools import create_tool_registry
from local_coder.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool
from local_coder.types import AgentRole, AgentTask, ModelResponse, ToolCall


def test_file_reads_and_known_edits_report_exact_source_digests(tmp_path):
    async def scenario():
        source = tmp_path / 'source.txt'
        source.write_bytes(b'first\r\nline\r\n')
        read = await ReadFileTool(str(tmp_path)).execute('source.txt')
        assert read.data['source_hashes']['source.txt'] == hashlib.sha256(source.read_bytes()).hexdigest()
        edited = await EditFileTool(str(tmp_path)).execute('source.txt', 'first', 'second')
        assert edited.success
        assert edited.data['source_hashes']['source.txt'] == hashlib.sha256(source.read_bytes()).hexdigest()
        written = await WriteFileTool(str(tmp_path)).execute('source.txt', 'known\nbytes\n')
        assert written.data['source_hashes']['source.txt'] == hashlib.sha256(source.read_bytes()).hexdigest()
    asyncio.run(scenario())


def test_external_edit_between_read_and_checkpoint_does_not_bless_new_source(tmp_path):
    original = b'print("before")\n'
    source = tmp_path / 'app.py'
    source.write_bytes(original)

    class Agent(BaseAgent):
        role = AgentRole.CODER
        system_prompt = 'Read the source.'

    class Model:
        config = SimpleNamespace(temperature=0., max_tokens=100, context_length=8192)
        count = 0
        async def generate(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                return ModelResponse(tool_calls=[ToolCall(id='read', name='read_file', arguments={'path': 'app.py'})])
            return ModelResponse(content='Read complete.')

    async def scenario():
        registry = create_tool_registry(str(tmp_path))
        registry.runtime = AgentRuntime(str(tmp_path))
        agent = Agent(Model(), registry)
        original_boundary = agent._checkpoint_boundary
        async def boundary():
            if agent.state and agent.state.files_read:
                source.write_bytes(b'print("external edit")\n')
            await original_boundary()
        agent._checkpoint_boundary = boundary
        await agent.execute(AgentTask(role=AgentRole.CODER, objective='Read app.py'))
        checkpoints = [json.loads(path.read_text()) for path in registry.runtime.state_dir.glob('checkpoint_*.json')]
        observed = [row['notes']['workspace_file_fingerprints'] for row in checkpoints if row['notes']['workspace_file_fingerprints']]
        assert observed
        assert observed[-1]['app.py'] == hashlib.sha256(original).hexdigest()
        with pytest.raises(ValueError, match='changed'):
            validate_workspace_file_fingerprints(str(tmp_path), observed[-1])
    asyncio.run(scenario())

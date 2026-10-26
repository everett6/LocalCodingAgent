import asyncio
import json
from types import SimpleNamespace
from local_coder.agents.base import BaseAgent
from local_coder.core.runtime import AgentRuntime
from local_coder.tools import create_tool_registry
from local_coder.types import AgentRole, Message


class Agent(BaseAgent):
    role=AgentRole.CODER
    system_prompt='Inspect'


def test_archive_recovery_is_bounded_owned_and_hides_protocol_blocks(tmp_path):
    registry=create_tool_registry(str(tmp_path));registry.runtime=AgentRuntime(str(tmp_path))
    agent=Agent(SimpleNamespace(),registry)
    reference=agent._archive_context([Message(role='user',content='Important original objective'),Message(role='assistant',content='Observation',reasoning_details=[{'data':'opaque-protocol-secret'}]),Message(role='tool',content='prefix '+('x'*20000)+' TARGET exact source evidence')])
    assert agent._archive_references==[reference]
    async def scenario():
        result=await registry.execute_tool(AgentRole.CODER,'recover_context',{'reference':reference,'query':'TARGET','limit':1000})
        assert result.success and 'exact source evidence' in result.output
        assert len(result.output)<1200 and 'opaque-protocol-secret' not in result.output
        foreign=await registry.execute_tool(AgentRole.SECURITY,'recover_context',{'reference':reference,'_caller_role':AgentRole.CODER})
        assert not foreign.success
        assert not (await registry.execute_tool(AgentRole.CODER,'recover_context',{'reference':'/etc/passwd'})).success
    asyncio.run(scenario())


def test_archive_integrity_is_checked_before_recovery(tmp_path):
    registry=create_tool_registry(str(tmp_path));registry.runtime=AgentRuntime(str(tmp_path))
    agent=Agent(SimpleNamespace(),registry)
    path=agent._archive_context([Message(role='user',content='Original')])
    from pathlib import Path
    Path(path).write_text(json.dumps([{'role':'user','content':'Changed'}]))
    result=asyncio.run(registry.execute_tool(AgentRole.CODER,'recover_context',{'reference':path}))
    assert not result.success and 'integrity' in result.error

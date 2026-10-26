import asyncio
import json
from types import SimpleNamespace

import pytest

from local_coder.context.allocation import ContextAllocation
from local_coder.tools.base import Tool, ToolRegistry
from local_coder.types import AgentRole, Message, ToolName, ToolResult


def test_segment_allocations_fail_closed_without_truncating_policy():
    allocation = ContextAllocation()
    messages = [Message(role="system", content="policy" * 100)]
    used, exceeded = allocation.check(messages, 1000)
    assert used["system"] > 150 and exceeded == ["system"]
    assert messages[0].content == "policy" * 100
    with pytest.raises(ValueError):
        ContextAllocation(system=20)


@pytest.mark.parametrize("arguments", [None, {"path": 1}, {}, {"path": "ok", "extra": True},
                                       {"path": "ok", "_caller_role": "orchestrator"}])
def test_gateway_rejects_malformed_arguments_before_execution(arguments):
    class Probe(Tool):
        name = ToolName.READ_FILE
        description = "Probe"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        async def execute(self, **kwargs):
            pytest.fail("Invalid arguments reached execution")
    registry = ToolRegistry()
    registry.register(Probe())
    result = asyncio.run(registry.execute_tool(AgentRole.CODER, "read_file", arguments))
    assert not result.success


def test_gateway_executes_valid_call():
    class Probe(Tool):
        name = ToolName.READ_FILE
        description = "Probe"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        async def execute(self, path):
            return ToolResult(success=True, output=path)
    registry = ToolRegistry()
    registry.register(Probe())
    assert asyncio.run(registry.execute_tool(AgentRole.CODER, "read_file", {"path": "ok"})).output == "ok"


def test_opt_in_snapshot_is_private_redacted_and_records_usage(tmp_path):
    from local_coder.agents.base import BaseAgent
    from local_coder.core.runtime import AgentRuntime
    from local_coder.types import AgentTask, ModelResponse
    class Registry:
        context_snapshots = True
        runtime = AgentRuntime(str(tmp_path))
        def get_schemas_for_role(self, role):
            return []
    class Model:
        config = SimpleNamespace(model_id="test", temperature=0, max_tokens=100)
        async def generate(self, messages, **kwargs):
            return ModelResponse(content="Completed", prompt_tokens=5, completion_tokens=2)
    class Agent(BaseAgent):
        role = AgentRole.CODER
        system_prompt = "Follow engine policy"
    events = []
    agent = Agent(Model(), Registry(), events.append)
    asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Inspect api_key=sk-" + "a" * 40)))
    snapshots = list(Registry.runtime.state_dir.glob("context-archives/*.json"))
    assert len(snapshots) == 1
    assert "sk-" + "a" * 40 not in snapshots[0].read_text()
    assert json.loads(snapshots[0].read_text())[0]["role"] == "system"
    assert snapshots[0].stat().st_mode & 0o077 == 0
    assert any(event.event_type == "context_snapshot" for event in events)

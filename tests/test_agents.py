"""Tests for the model-driven agent loop."""
import asyncio
from types import SimpleNamespace

from local_coder.agents.base import BaseAgent
from local_coder.types import (
    AgentRole,
    AgentTask,
    Message,
    ModelResponse,
    TaskStatus,
    ToolCall,
    ToolResult,
)


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.config = SimpleNamespace(temperature=0.0, max_tokens=100)

    async def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return next(self.responses)


class FakeRegistry:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def get_schemas_for_role(self, role):
        return [{"type": "function", "function": {"name": "read_file"}}]

    async def execute_tool(self, role, name, arguments):
        self.calls.append((role, name, arguments))
        return next(self.results)


class LoopAgent(BaseAgent):
    role = AgentRole.CODER
    system_prompt = "Use tools to complete the task."


def run(coro):
    return asyncio.run(coro)


def test_tool_loop_returns_files_and_tool_feedback():
    tool_call = ToolCall(name="read_file", arguments={"path": "src/app.py"})
    model = FakeModel([
        ModelResponse(
            content="I will inspect the file.",
            tool_calls=[tool_call],
            prompt_tokens=4,
            completion_tokens=2,
        ),
        ModelResponse(content="The task is complete.", prompt_tokens=5, completion_tokens=3),
    ])
    registry = FakeRegistry([
        ToolResult(success=True, output="print('hello')", files_changed=["src/app.py"]),
    ])
    agent = LoopAgent(model, registry)

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Inspect the app.")))

    assert response.status == TaskStatus.COMPLETED
    assert response.files_changed == ["src/app.py"]
    assert response.metrics.model_calls == 2
    assert response.metrics.tool_calls == 1
    assert registry.calls == [(AgentRole.CODER, "read_file", {"path": "src/app.py"})]
    assert model.calls[1][0][-1] == Message(
        role="tool",
        content="print('hello')",
        tool_call_id=tool_call.id,
        name="read_file",
    )


def test_agent_resets_metrics_between_tasks():
    model = FakeModel([
        ModelResponse(content="first", prompt_tokens=1),
        ModelResponse(content="second", prompt_tokens=2),
    ])
    agent = LoopAgent(model, FakeRegistry([]))

    run(agent.execute(AgentTask(role=AgentRole.CODER, objective="First")))
    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Second")))

    assert response.metrics.model_calls == 1
    assert response.metrics.prompt_tokens == 2


def test_agent_stops_after_max_iterations():
    model = FakeModel([
        ModelResponse(tool_calls=[ToolCall(name="read_file")]),
        ModelResponse(tool_calls=[ToolCall(name="read_file")]),
    ])
    agent = LoopAgent(model, FakeRegistry([
        ToolResult(success=True, output="one"),
        ToolResult(success=True, output="two"),
    ]))
    agent.max_iterations = 2

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Keep working")))

    assert response.status == TaskStatus.FAILED
    assert response.metrics.model_calls == 2
    assert "maximum tool-calling iterations" in response.issues[0]

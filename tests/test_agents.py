"""Tests for the model-driven agent loop."""
import asyncio
from types import SimpleNamespace

from local_coder.agents.base import BaseAgent
from local_coder.types import (
    AgentPhase,
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


class FakeDrafter:
    def __init__(self, prediction):
        self._prediction = prediction
        self.accept_calls = []
        self.reject_calls = []

    async def predict_edit(self, file_path, file_content, objective, context=""):
        return self._prediction

    def accept_prediction(self, prediction_id, latency_saved_ms=1.0):
        self.accept_calls.append(prediction_id)

    def reject_prediction(self, prediction_id, cost_ms=1.0):
        self.reject_calls.append(prediction_id)


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
    assert agent.state is not None
    assert agent.state.phase == AgentPhase.DONE
    assert agent.state.iteration == 2
    assert agent.state.files_read == {"src/app.py"}
    assert agent.state.files_changed == {"src/app.py"}
    assert agent.state.tool_calls[0] == tool_call
    assert agent.state.finished_at is not None
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


def test_failed_test_tool_marks_state_failed():
    model = FakeModel([
        ModelResponse(
            tool_calls=[ToolCall(name="run_tests", arguments={"test_path": "tests/test_app.py"})],
        ),
        ModelResponse(content="Tests need another fix."),
    ])
    agent = LoopAgent(model, FakeRegistry([
        ToolResult(success=False, output="AssertionError: expected 1, got 2"),
    ]))

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Fix the app.")))

    assert response.status == TaskStatus.FAILED
    assert response.tests_passed is False
    assert response.tests_run == ["tests/test_app.py"]
    assert agent.state is not None
    assert agent.state.phase == AgentPhase.FAILED
    assert agent.state.test_results[0].passed is False
    assert "AssertionError" in agent.state.errors[0]


def test_agent_stops_at_tool_call_budget():
    model = FakeModel([
        ModelResponse(tool_calls=[ToolCall(name="read_file")]),
        ModelResponse(tool_calls=[ToolCall(name="read_file")]),
    ])
    agent = LoopAgent(model, FakeRegistry([
        ToolResult(success=True, output="one"),
        ToolResult(success=True, output="two"),
    ]))
    agent.max_tool_calls = 1

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Inspect")))

    assert response.status == TaskStatus.FAILED
    assert "tool-call limit" in response.issues[0]
    assert agent.state is not None
    assert agent.state.tool_calls_used == 1


def test_stagnation_warning_nudges_after_repeated_identical_failures():
    """A small local model is prone to retrying an identical failing call
    instead of changing approach. After stagnation_warning_threshold (3)
    identical failures in a row, the loop must inject a corrective system
    message rather than silently repeating forever."""
    same_call = ToolCall(name="read_file", arguments={"path": "missing.py"})
    model = FakeModel([
        ModelResponse(tool_calls=[same_call]),
        ModelResponse(tool_calls=[same_call]),
        ModelResponse(tool_calls=[same_call]),
        ModelResponse(content="Giving up on that file."),
    ])
    agent = LoopAgent(model, FakeRegistry([
        ToolResult(success=False, output="No such file", error="ENOENT"),
        ToolResult(success=False, output="No such file", error="ENOENT"),
        ToolResult(success=False, output="No such file", error="ENOENT"),
    ]))

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Read a file")))

    fourth_call_messages = model.calls[3][0]
    assert any(
        m.role == "system" and "same arguments" in m.content and "3 times" in m.content
        for m in fourth_call_messages
    )
    assert response.status == TaskStatus.COMPLETED


def test_stagnation_abort_after_too_many_identical_failures():
    """Past stagnation_abort_threshold (5), give up instead of burning the
    rest of the iteration budget on a call that has never once succeeded."""
    same_call = ToolCall(name="read_file", arguments={"path": "missing.py"})
    model = FakeModel([ModelResponse(tool_calls=[same_call]) for _ in range(6)])
    agent = LoopAgent(model, FakeRegistry([
        ToolResult(success=False, output="No such file") for _ in range(5)
    ]))

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Read a file")))

    assert response.status == TaskStatus.FAILED
    assert "Repeated the same failing read_file call" in response.issues[0]
    assert agent.state is not None
    assert agent.state.phase == AgentPhase.FAILED
    assert len(model.calls) == 5  # aborted after the 5th identical failure, not all 6 queued responses


def test_stagnation_counter_resets_after_success_or_different_call():
    model = FakeModel([
        ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "a.py"})]),
        ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "a.py"})]),
        ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "b.py"})]),
        ModelResponse(content="Done."),
    ])
    agent = LoopAgent(model, FakeRegistry([
        ToolResult(success=False, output="fail"),
        ToolResult(success=False, output="fail"),
        ToolResult(success=True, output="ok"),
    ]))

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Read files")))

    assert response.status == TaskStatus.COMPLETED
    assert agent._repeated_failure_count == 0


def test_agent_stops_at_test_run_budget():
    model = FakeModel([
        ModelResponse(tool_calls=[ToolCall(name="run_tests")]),
        ModelResponse(tool_calls=[ToolCall(name="run_tests")]),
    ])
    agent = LoopAgent(model, FakeRegistry([
        ToolResult(success=False, output="first failure"),
        ToolResult(success=False, output="second failure"),
    ]))
    agent.max_test_runs = 1

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="Test")))

    assert response.status == TaskStatus.FAILED
    assert "test-run limit" in response.issues[0]
    assert agent.state is not None
    assert agent.state.test_runs == 1


def test_drafter_prediction_is_injected_and_accepted_when_file_matches():
    """The draft agent-loop wiring: previously SpeculativeDrafter was never
    called from BaseAgent at all -- only benchmarks touched it. This is the
    real integration: a prediction gets injected into the prompt before the
    first model call, and is recorded as accepted once the agent actually
    touches the predicted file."""
    from local_coder.speculative.drafter import DraftPrediction

    prediction = DraftPrediction(
        prediction_id="pred-1", prediction_type="edit", content="+    return 42",
        file_path="app.py", confidence=0.9,
    )
    tool_call = ToolCall(name="write_file", arguments={"path": "app.py", "content": "..."})
    model = FakeModel([
        ModelResponse(content="Applying the fix.", tool_calls=[tool_call]),
        ModelResponse(content="Done."),
    ])
    registry = FakeRegistry([
        ToolResult(success=True, output="def foo(): pass"),  # read_file for the draft
        ToolResult(success=True, output="written", files_changed=["app.py"]),  # write_file
    ])
    drafter = FakeDrafter(prediction)
    agent = LoopAgent(model, registry, drafter=drafter)

    response = run(agent.execute(
        AgentTask(role=AgentRole.CODER, objective="Fix app.py", files=["app.py"])
    ))

    assert response.status == TaskStatus.COMPLETED
    first_call_messages = model.calls[0][0]
    assert any("Speculative draft" in m.content and "app.py" in m.content for m in first_call_messages)
    assert drafter.accept_calls == ["pred-1"]
    assert drafter.reject_calls == []


def test_drafter_prediction_is_rejected_when_file_not_touched():
    from local_coder.speculative.drafter import DraftPrediction

    prediction = DraftPrediction(
        prediction_id="pred-2", prediction_type="edit", content="x",
        file_path="app.py", confidence=0.9,
    )
    model = FakeModel([ModelResponse(content="No changes needed.")])
    registry = FakeRegistry([
        ToolResult(success=True, output="def foo(): pass"),  # read_file for the draft
    ])
    drafter = FakeDrafter(prediction)
    agent = LoopAgent(model, registry, drafter=drafter)

    response = run(agent.execute(
        AgentTask(role=AgentRole.CODER, objective="Look at app.py", files=["app.py"])
    ))

    assert response.status == TaskStatus.COMPLETED
    assert drafter.reject_calls == ["pred-2"]
    assert drafter.accept_calls == []


def test_no_drafter_means_no_extra_tool_call():
    model = FakeModel([ModelResponse(content="Done.")])
    registry = FakeRegistry([])
    agent = LoopAgent(model, registry)  # drafter=None default

    response = run(agent.execute(
        AgentTask(role=AgentRole.CODER, objective="x", files=["app.py"])
    ))

    assert response.status == TaskStatus.COMPLETED
    assert registry.calls == []


def test_drafter_returning_no_prediction_does_not_inject_or_settle():
    class NoPredictionDrafter:
        async def predict_edit(self, *a, **k):
            return None

        def accept_prediction(self, *a, **k):
            raise AssertionError("should not be called")

        def reject_prediction(self, *a, **k):
            raise AssertionError("should not be called")

    model = FakeModel([ModelResponse(content="Done.")])
    registry = FakeRegistry([ToolResult(success=True, output="content")])
    agent = LoopAgent(model, registry, drafter=NoPredictionDrafter())

    response = run(agent.execute(
        AgentTask(role=AgentRole.CODER, objective="x", files=["app.py"])
    ))

    assert response.status == TaskStatus.COMPLETED
    first_call_messages = model.calls[0][0]
    assert not any("Speculative draft" in m.content for m in first_call_messages)


class FlakyModel:
    """Raises for the first `fail_count` calls, then returns from `responses`.

    Mirrors the real failure mode of a quantized local model emitting
    malformed tool-call JSON, which llama-server rejects with a 500 --
    model.generate() raises rather than returning a bad ModelResponse.
    """

    def __init__(self, responses, fail_count):
        self.responses = iter(responses)
        self.fail_count = fail_count
        self.calls = 0
        self.config = SimpleNamespace(temperature=0.0, max_tokens=100)

    async def generate(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise RuntimeError("Failed to parse tool call arguments as JSON")
        return next(self.responses)


def test_generation_retry_recovers_from_transient_failures():
    model = FlakyModel([ModelResponse(content="Done.")], fail_count=2)
    registry = FakeRegistry([])
    agent = LoopAgent(model, registry)
    agent.generation_retry_backoff_seconds = 0.0

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="x")))

    assert response.status == TaskStatus.COMPLETED
    assert model.calls == 3


def test_generation_retry_gives_up_after_exhausting_attempts():
    model = FlakyModel([], fail_count=99)
    registry = FakeRegistry([])
    agent = LoopAgent(model, registry)
    agent.generation_retry_limit = 2
    agent.generation_retry_backoff_seconds = 0.0

    response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="x")))

    assert response.status == TaskStatus.FAILED
    assert model.calls == 3
    assert "after 3 attempts" in agent.state.errors[0]


def test_generation_retry_raises_temperature_each_attempt():
    """A malformed-tool-call-JSON failure can be a near-deterministic mode
    at a given prompt -- retrying at the same temperature just reproduces
    it. Each retry should ask for a slightly hotter sample instead."""
    class RecordingFailModel:
        def __init__(self):
            self.config = SimpleNamespace(temperature=0.2, max_tokens=100)
            self.temperatures = []

        async def generate(self, messages, **kwargs):
            self.temperatures.append(kwargs["temperature"])
            raise RuntimeError("Failed to parse tool call arguments as JSON")

    model = RecordingFailModel()
    registry = FakeRegistry([])
    agent = LoopAgent(model, registry)
    agent.generation_retry_limit = 2
    agent.generation_retry_backoff_seconds = 0.0

    run(agent.execute(AgentTask(role=AgentRole.CODER, objective="x")))

    assert model.temperatures == [0.2, 0.35, 0.5]

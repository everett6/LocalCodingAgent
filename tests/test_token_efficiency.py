"""Tests for context budgeting: tool-output truncation, pruning, summarization."""
import asyncio
import json
from types import SimpleNamespace

from local_coder.agents.base import BaseAgent
from local_coder.context.compression import (
    PRUNED_TOOL_OUTPUT,
    SUMMARY_PREFIX,
    SUMMARY_SYSTEM_PROMPT,
    SUPERSEDED_TOOL_OUTPUT,
    compress_messages,
    prune_tool_outputs,
    summarize_history,
    truncate_tool_output,
)
from local_coder.models.base import BaseModelBackend
from local_coder.models.ollama import OllamaBackend
from local_coder.tools import create_tool_registry
from local_coder.tools.filesystem import ReadFileTool
from local_coder.tools.search import GrepTool
from local_coder.types import (
    AgentRole,
    AgentTask,
    Message,
    ModelConfig,
    ModelResponse,
    TaskStatus,
    ToolCall,
    ToolResult,
)


def run(coro):
    return asyncio.run(coro)


def call(call_id, name, **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


def conversation(*turns):
    """system + user, then (tool_call, output) pairs as assistant/tool messages."""
    messages = [Message(role="system", content="sys"), Message(role="user", content="task")]
    for tool_call, output in turns:
        messages.append(Message(role="assistant", content="", tool_calls=[tool_call]))
        messages.append(Message(role="tool", content=output, tool_call_id=tool_call.id, name=tool_call.name))
    return messages


class TestTruncateToolOutput:
    def test_small_output_is_unchanged(self):
        assert truncate_tool_output("ok", max_chars=100, max_lines=10) == "ok"

    def test_keeps_head_and_tail_and_saves_full_text(self, tmp_path):
        text = "\n".join(f"line {i}" for i in range(1000))
        spill = tmp_path / "out.txt"

        result = truncate_tool_output(text, max_chars=500, max_lines=50, spill_path=spill, spill_display="out.txt")

        assert result.startswith("line 0\n")
        assert result.endswith("line 999")
        assert "lines" in result and "omitted" in result
        assert "saved to out.txt" in result
        assert len(result) < 800
        assert spill.read_text() == text

    def test_single_huge_line_still_shows_something(self):
        result = truncate_tool_output("x" * 10000, max_chars=100, max_lines=10)
        assert result.startswith("x" * 60)
        assert len(result) < 200


class TestPruneToolOutputs:
    def test_clears_superseded_identical_reads(self):
        read = lambda call_id: call(call_id, "read_file", path="a.py")
        old = "old contents " * 20
        messages = conversation((read("1"), old), (call("2", "run_tests"), "ok"), (read("3"), "new contents"))

        pruned, freed = prune_tool_outputs(messages, protect_chars=10_000)

        assert pruned[3].content == SUPERSEDED_TOOL_OUTPUT
        assert pruned[5].content == "ok"
        assert pruned[-1].content == "new contents"
        assert freed == len(old) - len(SUPERSEDED_TOOL_OUTPUT)

    def test_clears_outputs_older_than_protected_window(self):
        messages = conversation(
            (call("1", "run_tests"), "a" * 500),
            (call("2", "run_tests"), "b" * 500),
            (call("3", "run_tests"), "c" * 500),
        )

        pruned, freed = prune_tool_outputs(messages, protect_chars=1200)

        assert pruned[3].content == PRUNED_TOOL_OUTPUT
        assert pruned[5].content == "b" * 500
        assert pruned[7].content == "c" * 500
        assert freed == 500 - len(PRUNED_TOOL_OUTPUT)
        # Structure is untouched: same length, roles and ids.
        assert [(m.role, m.tool_call_id) for m in pruned] == [(m.role, m.tool_call_id) for m in messages]


class TestCompressMessages:
    def test_never_starts_recent_history_with_orphaned_tool_result(self):
        messages = conversation((call("1", "grep", pattern="x"), "g" * 200), (call("2", "grep", pattern="y"), "h" * 50))

        compacted = compress_messages(messages, 120)

        after_marker = compacted[3:]
        assert not after_marker or after_marker[0].role != "tool"


class SummaryModel:
    def __init__(self, summary="Read a.py; tests failing in test_a."):
        self.summary = summary
        self.prompts = []
        self.config = SimpleNamespace(temperature=0.0, max_tokens=100)

    async def generate(self, messages, **kwargs):
        self.prompts.append(messages)
        return ModelResponse(content=self.summary, prompt_tokens=50, completion_tokens=10)


class TestSummarizeHistory:
    def test_replaces_middle_and_keeps_recent_turn_intact(self):
        messages = conversation(
            (call("1", "read_file", path="a.py"), "a" * 1000),
            (call("2", "run_tests"), "FAILED test_a"),
        )
        model = SummaryModel()

        new_messages, response = run(summarize_history(model, messages, keep_recent_chars=100, max_input_chars=5000))

        assert [m.content for m in new_messages[:2]] == ["sys", "task"]
        assert new_messages[2].role == "user" and new_messages[2].content.startswith(SUMMARY_PREFIX)
        assert new_messages[3].role == "assistant" and new_messages[3].tool_calls[0].id == "2"
        assert new_messages[4].content == "FAILED test_a"
        assert "a.py" in model.prompts[0][1].content
        assert response.prompt_tokens == 50

    def test_strips_reasoning_and_returns_none_when_empty(self):
        messages = conversation((call("1", "run_tests"), "x" * 1000), (call("2", "run_tests"), "y"))
        model = SummaryModel(summary="<think>hmm</think>")

        assert run(summarize_history(model, messages, keep_recent_chars=100, max_input_chars=5000)) is None


class ScriptedModel:
    def __init__(self, responses, context_length=None):
        self.responses = iter(responses)
        self.calls = []
        self.config = SimpleNamespace(temperature=0.0, max_tokens=100, context_length=context_length)

    async def generate(self, messages, **kwargs):
        self.calls.append((list(messages), kwargs))
        return next(self.responses)


class ScriptedRegistry:
    def __init__(self, outputs):
        self.outputs = iter(outputs)

    def get_schemas_for_role(self, role):
        return []

    async def execute_tool(self, role, name, arguments):
        return ToolResult(success=True, output=next(self.outputs))


class LoopAgent(BaseAgent):
    role = AgentRole.CODER
    system_prompt = "Use tools."


def tool_turn(call_id, content=""):
    return ModelResponse(content=content, tool_calls=[call(call_id, "run_tests")], prompt_tokens=10)


class TestAgentLoopBudget:
    def test_large_tool_output_is_truncated_before_reaching_the_model(self):
        model = ScriptedModel([tool_turn("1"), ModelResponse(content="done")])
        agent = LoopAgent(model, ScriptedRegistry(["z" * 50_000]), max_tool_output_chars=2000)

        response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="x")))

        assert response.status == TaskStatus.COMPLETED
        tool_message = model.calls[1][0][-1]
        assert tool_message.role == "tool"
        assert len(tool_message.content) < 2500

    def test_prunes_old_tool_output_before_asking_for_a_summary(self):
        model = ScriptedModel([tool_turn("1"), tool_turn("2"), ModelResponse(content="done")])
        agent = LoopAgent(
            model,
            ScriptedRegistry(["z" * 1500] * 2),
            context_window_chars=3000,
            compact_context_chars=2000,
            max_tool_output_chars=5000,
        )
        agent.max_tool_output_lines = 10_000

        response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="x")))

        # Three generate() calls: no summary request was needed.
        assert len(model.calls) == 3
        assert response.status == TaskStatus.COMPLETED
        assert any(m.content == PRUNED_TOOL_OUTPUT for m in model.calls[-1][0])

    def test_summarizes_when_pruning_is_not_enough_and_counts_the_call(self):
        # The bulk is the model's own reasoning text, which pruning can't touch.
        summary = ModelResponse(content="Ran tests twice; they pass.", prompt_tokens=7, completion_tokens=3)
        model = ScriptedModel([
            tool_turn("1", "r" * 1500), tool_turn("2", "s" * 1500), summary, ModelResponse(content="done"),
        ])
        agent = LoopAgent(model, ScriptedRegistry(["ok", "ok"]), context_window_chars=3000, compact_context_chars=2000)

        response = run(agent.execute(AgentTask(role=AgentRole.CODER, objective="x")))

        assert response.status == TaskStatus.COMPLETED
        assert model.calls[2][0][0].content == SUMMARY_SYSTEM_PROMPT
        final_prompt = model.calls[-1][0]
        assert final_prompt[0].content == "Use tools."
        assert any(m.content.startswith(SUMMARY_PREFIX) for m in final_prompt)
        assert sum(len(m.content) for m in final_prompt) <= 2000
        assert response.metrics.model_calls == 4
        assert response.metrics.prompt_tokens == 10 + 10 + 7

    def test_server_reported_tokens_trigger_compaction(self):
        # Well under the 24000-char default budget, but the server says the
        # prompt is at 950 of 1000 tokens.
        near_limit = ModelResponse(content="", tool_calls=[call("2", "run_tests")], prompt_tokens=950)
        model = ScriptedModel(
            [tool_turn("1"), near_limit, ModelResponse(content="done")],
            context_length=1000,
        )
        agent = LoopAgent(model, ScriptedRegistry(["a" * 800, "b" * 800]))

        run(agent.execute(AgentTask(role=AgentRole.CODER, objective="x")))

        assert any(m.content == PRUNED_TOOL_OUTPUT for m in model.calls[-1][0])


class TestReadFilePaging:
    def test_pages_large_files_with_continuation_hint(self, tmp_path):
        (tmp_path / "big.py").write_text("".join(f"x = {i}\n" for i in range(3000)))
        tool = ReadFileTool(str(tmp_path))

        first = run(tool.execute(path="big.py"))
        assert first.output.startswith("x = 0\n")
        assert "Showing lines 1-1000 of 3000" in first.output
        assert "start_line=1001" in first.output

        second = run(tool.execute(path="big.py", start_line=1001))
        assert second.output.startswith("x = 1000\n")

    def test_small_file_has_no_footer(self, tmp_path):
        (tmp_path / "a.py").write_text("print(1)\n")
        assert run(ReadFileTool(str(tmp_path)).execute(path="a.py")).output == "print(1)\n"

    def test_long_lines_are_clipped(self, tmp_path):
        (tmp_path / "min.js").write_text("a" * 50_000 + "\nend\n")
        output = run(ReadFileTool(str(tmp_path)).execute(path="min.js")).output
        assert "line truncated" in output
        assert output.endswith("end\n")

    def test_start_past_end(self, tmp_path):
        (tmp_path / "a.py").write_text("one\n")
        assert "past the end" in run(ReadFileTool(str(tmp_path)).execute(path="a.py", start_line=5)).output


def test_grep_clips_very_long_matching_lines(tmp_path):
    (tmp_path / "bundle.js").write_text("needle" + "q" * 5000 + "\n")
    output = run(GrepTool(str(tmp_path)).execute(pattern="needle")).output
    assert len(output) < 500 and "chars]" in output


def test_agent_spills_truncated_output_where_read_file_can_reach_it(tmp_path):
    registry = create_tool_registry(str(tmp_path))
    agent = LoopAgent(ScriptedModel([]), registry, max_tool_output_chars=1000)

    text = agent._fit_tool_output(call("abc", "run_tests"), "\n".join(str(i) for i in range(5000)))

    spilled = tmp_path / ".local-coder" / "tool-output" / "run_tests-abc.txt"
    assert spilled.exists()
    assert ".local-coder/tool-output/run_tests-abc.txt" in text
    page = run(registry.execute_tool(AgentRole.CODER, "read_file", {"path": ".local-coder/tool-output/run_tests-abc.txt"}))
    assert page.success and page.output.startswith("0\n")


def test_tool_call_arguments_are_sent_as_json():
    class Backend(BaseModelBackend):
        async def generate(self, *a, **k): ...
        async def stream(self, *a, **k): ...
        async def is_available(self): ...

    config = ModelConfig(name="m", model_id="m")
    message = Message(role="assistant", content="", tool_calls=[call("1", "read_file", path="a.py")])

    encoded = Backend(config)._build_messages([message])[0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(encoded) == {"path": "a.py"}
    assert OllamaBackend(config)._build_messages([message])[0]["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}

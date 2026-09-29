"""Tests for tool-call repair, read-before-overwrite, and post-edit syntax checks."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from local_coder.agents.base import BaseAgent
from local_coder.agents.tool_repair import (
    RAW_ARGUMENTS_KEY,
    extract_text_tool_calls,
    normalize_arguments,
    parse_arguments,
    repair_tool_call,
    resolve_tool_name,
    schemas_by_name,
)
from local_coder.models.base import decode_tool_arguments
from local_coder.tools import create_tool_registry
from local_coder.types import AgentRole, AgentTask, ModelResponse, TaskStatus, ToolCall
from local_coder.verification.syntax import check_syntax

TOOLS = {"read_file", "write_file", "list_files", "grep", "run_command", "run_tests", "git_status", "git_diff"}


# --- argument parsing ---------------------------------------------------

def test_parse_arguments_accepts_json_and_common_near_misses():
    assert parse_arguments('{"path": "a.py"}') == {"path": "a.py"}
    assert parse_arguments("{'path': 'a.py', 'recursive': True}") == {"path": "a.py", "recursive": True}
    assert parse_arguments('{"path": "a.py",}') == {"path": "a.py"}
    assert parse_arguments('```json\n{"path": "a.py"}\n```') == {"path": "a.py"}
    assert parse_arguments('{"path": "a.py", "content": "x = 1') == {"path": "a.py", "content": "x = 1"}
    assert parse_arguments("") == {}


def test_parse_arguments_rejects_non_objects():
    assert parse_arguments("read the file please") is None
    assert parse_arguments("[1, 2]") is None


def test_decode_tool_arguments_keeps_unrepairable_text():
    assert decode_tool_arguments('{"path": "a.py"}') == {"path": "a.py"}
    assert decode_tool_arguments({"path": "a.py"}) == {"path": "a.py"}
    assert decode_tool_arguments("path=a.py") == {RAW_ARGUMENTS_KEY: "path=a.py"}


# --- tool names and arguments -------------------------------------------

def test_resolve_tool_name_handles_case_aliases_and_typos():
    assert resolve_tool_name("read_file", TOOLS) == "read_file"
    assert resolve_tool_name("ReadFile", TOOLS) == "read_file"
    assert resolve_tool_name("read-file", TOOLS) == "read_file"
    assert resolve_tool_name("functions.read_file", TOOLS) == "read_file"
    assert resolve_tool_name("bash", TOOLS) == "run_command"
    assert resolve_tool_name("read_fiel", TOOLS) == "read_file"
    assert resolve_tool_name("edit", TOOLS) is None  # alias target not available
    assert resolve_tool_name("deploy", TOOLS) is None


def test_normalize_arguments_renames_aliases_and_coerces_types():
    schema = {
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean"},
            "start_line": {"type": "integer"},
        }
    }
    args, notes = normalize_arguments({"file_path": "src", "recursive": "true", "start_line": "12"}, schema)
    assert args == {"path": "src", "recursive": True, "start_line": 12}
    assert len(notes) == 3

    args, _ = normalize_arguments({"arguments": {"path": "src"}}, schema)
    assert args == {"path": "src"}


def test_repair_tool_call_reports_missing_and_unparseable_arguments():
    schemas = {"read_file": {"properties": {"path": {"type": "string"}}, "required": ["path"]}}

    missing = repair_tool_call(ToolCall(name="ReadFile", arguments={}), schemas)
    assert missing.call.name == "read_file"
    assert "missing required argument(s): path" in missing.error

    raw = repair_tool_call(ToolCall(name="read_file", arguments={RAW_ARGUMENTS_KEY: "path=x"}), schemas)
    assert "not valid JSON" in raw.error
    assert RAW_ARGUMENTS_KEY not in raw.call.arguments

    ok = repair_tool_call(ToolCall(name="read_file", arguments={"filename": "x.py"}), schemas)
    assert ok.error is None
    assert ok.call.arguments == {"path": "x.py"}


def test_unknown_tool_names_pass_through_for_the_registry_to_reject():
    repaired = repair_tool_call(ToolCall(name="deploy", arguments={}), {"read_file": {}})
    assert repaired.error is None
    assert repaired.call.name == "deploy"


# --- tool calls written as text -----------------------------------------

def test_extracts_hermes_style_tagged_calls():
    content = (
        "Let me look.\n<tool_call>\n{\"name\": \"read_file\", \"arguments\": {\"path\": \"a.py\"}}\n</tool_call>\n"
        "<tool_call>{\"name\": \"grep\", \"arguments\": {\"pattern\": \"TODO\"}}</tool_call>"
    )
    calls = extract_text_tool_calls(content, TOOLS)
    assert [(c.name, c.arguments) for c in calls] == [
        ("read_file", {"path": "a.py"}),
        ("grep", {"pattern": "TODO"}),
    ]


def test_extracts_function_tag_and_whole_message_json():
    calls = extract_text_tool_calls('<function=read_file>{"path": "a.py"}</function>', TOOLS)
    assert [(c.name, c.arguments) for c in calls] == [("read_file", {"path": "a.py"})]

    calls = extract_text_tool_calls('```json\n{"name": "git_status", "arguments": {}}\n```', TOOLS)
    assert [c.name for c in calls] == ["git_status"]

    calls = extract_text_tool_calls('{"tool": "read_file", "parameters": "{\\"path\\": \\"b.py\\"}"}', TOOLS)
    assert [(c.name, c.arguments) for c in calls] == [("read_file", {"path": "b.py"})]


def test_does_not_execute_example_calls_inside_prose():
    content = 'Done. Next time you can run {"name": "run_tests", "arguments": {}} yourself.'
    assert extract_text_tool_calls(content, TOOLS) == []
    assert extract_text_tool_calls("All tests pass.", TOOLS) == []
    assert extract_text_tool_calls('{"name": "deploy", "arguments": {}}', TOOLS) == []


# --- syntax checks ------------------------------------------------------

def test_check_syntax_reports_line_and_excerpt(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def f():\n    return (1,\n\nx = 2\n")
    report = check_syntax(bad, display_path="bad.py")
    assert report is not None and report.startswith("bad.py:")
    assert "|" in report

    (tmp_path / "ok.py").write_text("x = 1\n")
    assert check_syntax(tmp_path / "ok.py") is None


def test_check_syntax_covers_data_formats(tmp_path):
    cases = {
        "a.json": ('{"a": 1,}', '{"a": 1}'),
        "a.toml": ("a = [1,", "a = [1]"),
        "a.yaml": ("a: [1, 2\nb: 3\n", "a: [1, 2]\n"),
    }
    for name, (broken, fine) in cases.items():
        path = tmp_path / name
        path.write_text(broken)
        assert check_syntax(path, display_path=name) is not None, name
        path.write_text(fine)
        assert check_syntax(path) is None, name

    (tmp_path / "notes.txt").write_text("{{{")
    assert check_syntax(tmp_path / "notes.txt") is None


# --- agent loop integration ---------------------------------------------

class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.config = SimpleNamespace(temperature=0.0, max_tokens=100)

    async def generate(self, messages, **kwargs):
        self.calls.append(messages)
        return next(self.responses)


class CoderLoop(BaseAgent):
    role = AgentRole.CODER
    system_prompt = "Use tools."


def _run(tmp_path: Path, responses):
    model = ScriptedModel(responses)
    agent = CoderLoop(model, create_tool_registry(str(tmp_path)))
    response = asyncio.run(agent.execute(AgentTask(role=AgentRole.CODER, objective="work")))
    tool_messages = [m for messages in model.calls for m in messages if m.role == "tool"]
    # Each model call re-sends history; keep the unique tool results in order.
    seen, unique = set(), []
    for message in tool_messages:
        if message.tool_call_id not in seen:
            seen.add(message.tool_call_id)
            unique.append(message.content)
    return agent, response, unique


def test_agent_runs_tool_calls_written_as_text(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    agent, response, outputs = _run(tmp_path, [
        ModelResponse(content='<tool_call>{"name": "ReadFile", "arguments": {"file_path": "a.py"}}</tool_call>'),
        ModelResponse(content="Done."),
    ])
    assert response.status == TaskStatus.COMPLETED
    assert agent.state.tool_calls[0].name == "read_file"
    assert "x = 1" in outputs[0]
    assert "interpreted tool 'ReadFile' as 'read_file'" in outputs[0]
    assert agent.state.files_read == {"a.py"}


def test_agent_reports_unparseable_arguments_without_running_the_tool(tmp_path):
    agent, _, outputs = _run(tmp_path, [
        ModelResponse(tool_calls=[ToolCall(name="write_file", arguments={RAW_ARGUMENTS_KEY: "path=a.py"})]),
        ModelResponse(content="Done."),
    ])
    assert "not valid JSON" in outputs[0]
    assert not (tmp_path / "a.py").exists()


def test_write_file_must_read_existing_file_first(tmp_path):
    target = tmp_path / "keep.py"
    target.write_text("important = True\n")
    _, response, outputs = _run(tmp_path, [
        ModelResponse(tool_calls=[ToolCall(name="write_file", arguments={"path": "keep.py", "content": "x = 1\n"})]),
        ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "./keep.py"})]),
        ModelResponse(tool_calls=[ToolCall(name="write_file", arguments={"path": "keep.py", "content": "x = 1\n"})]),
        ModelResponse(content="Done."),
    ])
    assert "Refusing to overwrite keep.py" in outputs[0]
    assert outputs[2] == "Wrote to keep.py"
    assert target.read_text() == "x = 1\n"
    assert response.files_changed == ["keep.py"]


def test_new_files_and_files_the_agent_wrote_need_no_read(tmp_path):
    _, _, outputs = _run(tmp_path, [
        ModelResponse(tool_calls=[ToolCall(name="write_file", arguments={"path": "new.py", "content": "a = 1\n"})]),
        ModelResponse(tool_calls=[ToolCall(name="write_file", arguments={"path": "new.py", "content": "a = 2\n"})]),
        ModelResponse(content="Done."),
    ])
    assert outputs == ["Wrote to new.py", "Wrote to new.py"]


def test_syntax_error_is_reported_in_the_same_turn(tmp_path):
    _, _, outputs = _run(tmp_path, [
        ModelResponse(tool_calls=[ToolCall(name="write_file", arguments={"path": "bad.py", "content": "def f(:\n"})]),
        ModelResponse(content="Done."),
    ])
    assert outputs[0].startswith("Wrote to bad.py")
    assert "no longer parses" in outputs[0]
    assert "bad.py:1" in outputs[0]


def test_schemas_by_name_indexes_registry_schemas(tmp_path):
    schemas = schemas_by_name(create_tool_registry(str(tmp_path)).get_schemas_for_role(AgentRole.CODER))
    assert schemas["read_file"]["required"] == ["path"]

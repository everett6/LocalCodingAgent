"""--json output mode: one parseable envelope on stdout, errors included."""
import json
import subprocess

import pytest
from click.testing import CliRunner

from local_coder.cli.main import cli
from local_coder.cli.output import SCHEMA_VERSION
from local_coder.types import AgentEvent


def invoke(*args, input=None):
    result = CliRunner().invoke(cli, list(args), input=input)
    # stdout must be exactly one JSON document; panels/events go to stderr.
    return result, json.loads(result.stdout)


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("print('hi')  # TODO tidy\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_envelope_shape_and_flag_position(tmp_path):
    for args in (["--json", "--project", str(tmp_path), "models"],
                 ["--project", str(tmp_path), "models", "--json"]):
        result, doc = invoke(*args)
        assert result.exit_code == 0
        assert set(doc) == {"schema_version", "command", "ok", "data", "error"}
        assert doc["schema_version"] == SCHEMA_VERSION
        assert doc["command"] == "models"
        assert doc["ok"] is True and doc["error"] is None
        assert isinstance(doc["data"]["models"], list)


def test_without_flag_output_is_unchanged(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "agents"])
    assert result.exit_code == 0
    assert "Configured Agents" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_agents(tmp_path):
    _, doc = invoke("--project", str(tmp_path), "agents", "--json")
    roles = {agent["role"] for agent in doc["data"]["agents"]}
    assert {"planner", "coder", "reviewer", "security"} <= roles
    assert all("system_prompt" in agent and "model" in agent for agent in doc["data"]["agents"])


def test_status(repo):
    _, doc = invoke("--project", str(repo), "status", "--json")
    data = doc["data"]
    assert data["project_root"] == str(repo)
    assert data["git_branch"] is not None
    assert set(data["ollama"]) == {"reachable", "status_code"}


def test_config(repo):
    _, doc = invoke("--project", str(repo), "config", "--json")
    data = doc["data"]
    assert "sources" in data and "routing" in data and "tools" in data
    assert isinstance(data["problems"], list)


def test_init_and_existing_config_error(tmp_path):
    result, doc = invoke("--project", str(tmp_path), "init", "--json")
    assert result.exit_code == 0
    assert doc["data"]["config_path"].endswith("config.yaml")

    result, doc = invoke("--project", str(tmp_path), "init", "--json")
    assert result.exit_code == 1
    assert doc["ok"] is False and doc["data"] is None
    assert doc["error"]["type"] == "ClickException"
    assert "already exists" in doc["error"]["message"]


def test_checkpoints(repo):
    (repo / "app.py").write_text("print('changed')\n")
    _, created = invoke("--project", str(repo), "checkpoint", "--json")
    checkpoint_id = created["data"]["checkpoint"]["checkpoint_id"]

    _, listed = invoke("--project", str(repo), "checkpoints", "--json")
    assert [c["checkpoint_id"] for c in listed["data"]["checkpoints"]] == [checkpoint_id]

    _, restored = invoke("--project", str(repo), "rollback", checkpoint_id, "--json")
    assert restored["command"] == "rollback"
    assert restored["data"]["checkpoint"]["checkpoint_id"] == checkpoint_id


def test_map(repo):
    _, doc = invoke("--project", str(repo), "map", "--json")
    assert doc["command"] == "map" and "app.py" in doc["data"]["map"]


def test_sessions_empty(tmp_path):
    _, doc = invoke("--project", str(tmp_path), "sessions", "--json")
    assert doc["data"]["sessions"] == []


def test_local_server_status(monkeypatch):
    from local_coder import local_server

    monkeypatch.setattr(local_server, "status", lambda: {"big": {"pid": None, "port": 8090, "healthy": False}})
    _, doc = invoke("local-server", "status", "--json")
    assert doc["command"] == "local-server status"
    assert doc["data"]["servers"]["big"]["port"] == 8090


def test_tools_lists_schemas_and_roles(tmp_path):
    _, doc = invoke("--project", str(tmp_path), "tools", "--json")
    tools = {tool["name"]: tool for tool in doc["data"]["tools"]}
    # Commands that landed on main are reachable as tools with JSON output.
    assert {"run_tests", "grep", "code_search", "lint"} <= set(tools)
    assert tools["grep"]["parameters"]["type"] == "object"
    assert "reviewer" in tools["grep"]["roles"]
    assert "reviewer" not in tools["write_file"]["roles"]


def test_tool_runs_directly(repo):
    result, doc = invoke("--project", str(repo), "tool", "grep", "-a", "pattern=TODO", "--json")
    assert result.exit_code == 0
    data = doc["data"]
    assert data["tool"] == "grep" and data["arguments"] == {"pattern": "TODO"}
    assert data["success"] is True and "app.py" in data["output"]


def test_tool_json_args_and_value_parsing(repo):
    _, doc = invoke("--project", str(repo), "tool", "read_file",
                    "--args", '{"path": "app.py"}', "-a", "start_line=1", "--json")
    assert doc["data"]["arguments"] == {"path": "app.py", "start_line": 1}
    assert "print" in doc["data"]["output"]


def test_tool_failure_exits_nonzero_with_data(repo):
    result, doc = invoke("--project", str(repo), "tool", "read_file", "-a", "path=missing.py", "--json")
    assert result.exit_code == 1
    assert doc["ok"] is False and doc["error"] is None
    assert doc["data"]["success"] is False


def test_tool_errors_are_envelopes(repo):
    result, doc = invoke("--project", str(repo), "tool", "nope", "--json")
    assert result.exit_code == 1
    assert doc["error"]["type"] == "BadParameter" and "run_tests" in doc["error"]["message"]

    result, doc = invoke("--project", str(repo), "tool", "grep", "--args", "[1]", "--json")
    assert result.exit_code == 1 and "JSON object" in doc["error"]["message"]


def test_risky_tool_is_denied_without_terminal(repo):
    """No TTY to confirm on: deny rather than hang or abort, prompt on stderr."""
    result, doc = invoke("--project", str(repo), "tool", "run_command", "-a", "command=echo hi", "--json", input="")
    assert doc["data"]["success"] is False
    assert "denied" in doc["data"]["output"].lower()
    assert "Approval required" in result.stderr


def test_run_tests_tool_details_parse_failures(repo, monkeypatch):
    from local_coder.tools.testing import RunTestsTool
    from local_coder.types import ToolResult

    async def fake_execute(self, **kwargs):
        return ToolResult(success=False, output="FAILED tests/test_a.py::test_x - assert 1 == 2\n")

    monkeypatch.setattr(RunTestsTool, "execute", fake_execute)
    result, doc = invoke("--project", str(repo), "tool", "run_tests", "--json")
    assert result.exit_code == 1
    assert doc["data"]["details"] == {"failures": [{"test": "tests/test_a.py::test_x", "message": "assert 1 == 2"}]}


class FakeCoordinator:
    """Stands in for the real Coordinator so agent-run JSON can be tested
    without a model. Matches the calls _build_coordinator/_run_request make."""

    final_event = "task_completed"
    extra_events: list = []

    def __init__(self, **kwargs):
        self.handlers = []
        self.tool_registry = None

    def on_event(self, handler):
        self.handlers.append(handler)

    def _emit(self, source, event_type, message):
        for handler in self.handlers:
            handler(AgentEvent(source=source, event_type=event_type, message=message))

    async def run(self, request, **kwargs):
        self._emit("ORCHESTRATOR", "task_started", f"Processing: {request}")
        for source, event_type, message in self.extra_events:
            self._emit(source, event_type, message)
        self._emit("ORCHESTRATOR", self.final_event, "Done")
        return f"Result for {request}"

    async def run_tests_only(self):
        return "1 passed, 0 failed"


@pytest.fixture
def fake_coordinator(monkeypatch):
    import local_coder.orchestrator.coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "Coordinator", FakeCoordinator)
    monkeypatch.setattr(FakeCoordinator, "extra_events", [])
    monkeypatch.setattr(FakeCoordinator, "final_event", "task_completed")
    return FakeCoordinator


def test_run_emits_result_session_and_events(tmp_path, fake_coordinator):
    result, doc = invoke("--project", str(tmp_path), "--json", "run", "add", "a", "test")
    assert result.exit_code == 0
    data = doc["data"]
    assert doc["command"] == "run"
    assert data["request"] == "add a test" and data["phase"] == "run"
    assert data["status"] == "completed"
    assert data["result"] == "Result for add a test"
    assert data["session_id"]
    assert [e["event_type"] for e in data["events"]] == ["task_started", "task_completed"]
    # Human progress lines still go somewhere, just not stdout.
    assert "Processing: add a test" in result.stderr


def test_run_with_model_error_is_not_ok(tmp_path, fake_coordinator, monkeypatch):
    monkeypatch.setattr(FakeCoordinator, "extra_events", [("CODER", "model_error", "connection refused")])
    result, doc = invoke("--project", str(tmp_path), "--json", "run", "x")
    assert result.exit_code == 1
    assert doc["ok"] is False
    assert doc["data"]["status"] == "completed"
    assert [e["message"] for e in doc["data"]["errors"]] == ["connection refused"]


def test_plan_and_review_emit(tmp_path, fake_coordinator):
    _, plan = invoke("--project", str(tmp_path), "plan", "do a thing", "--json")
    assert plan["command"] == "plan" and plan["data"]["phase"] == "plan"
    assert plan["data"]["result"].startswith("Result for Create a detailed plan")

    _, review = invoke("--project", str(tmp_path), "review", "--json")
    assert review["data"]["phase"] == "review" and review["ok"] is True


def test_test_command_emits(tmp_path, fake_coordinator):
    _, doc = invoke("--project", str(tmp_path), "test", "--json")
    assert doc["command"] == "test"
    assert doc["data"]["phase"] == "test"
    assert doc["data"]["result"] == "1 passed, 0 failed"


def test_interactive_mode_rejects_json(tmp_path):
    result, doc = invoke("--project", str(tmp_path), "--json")
    assert result.exit_code == 1
    assert doc["error"]["type"] == "UsageError"

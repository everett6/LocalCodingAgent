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
    assert {"planner", "coder", "reviewer"} <= roles
    assert all("system_prompt" in agent and "model" in agent for agent in doc["data"]["agents"])


def test_status(repo):
    _, doc = invoke("--project", str(repo), "status", "--json")
    data = doc["data"]
    assert data["project_root"] == str(repo)
    assert data["git_branch"] is not None
    assert set(data["ollama"]) == {"reachable", "status_code"}


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


def test_local_server_status(monkeypatch):
    from local_coder import local_server

    monkeypatch.setattr(local_server, "status", lambda: {"big": {"pid": None, "port": 8090, "healthy": False}})
    _, doc = invoke("local-server", "status", "--json")
    assert doc["command"] == "local-server status"
    assert doc["data"]["servers"]["big"]["port"] == 8090


def test_tools_lists_schemas_and_roles(tmp_path):
    _, doc = invoke("--project", str(tmp_path), "tools", "--json")
    tools = {tool["name"]: tool for tool in doc["data"]["tools"]}
    assert "run_tests" in tools
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


def test_run_tests_details_parse_failures(repo, monkeypatch):
    from local_coder.tools.testing import RunTestsTool
    from local_coder.types import ToolResult

    async def fake_execute(self, **kwargs):
        return ToolResult(success=False, output="FAILED tests/test_a.py::test_x - assert 1 == 2\n")

    monkeypatch.setattr(RunTestsTool, "execute", fake_execute)
    result, doc = invoke("--project", str(repo), "tool", "run_tests", "--json")
    assert result.exit_code == 1
    assert doc["data"]["details"] == {"failures": [{"test": "tests/test_a.py::test_x", "message": "assert 1 == 2"}]}


class FakeCoordinator:
    final_event = "task_completed"
    extra_events: list = []

    def __init__(self, **kwargs):
        self.handlers = []

    def on_event(self, handler):
        self.handlers.append(handler)

    async def run(self, request):
        events = [("ORCHESTRATOR", "task_started", f"Processing: {request}"), *self.extra_events,
                  ("ORCHESTRATOR", self.final_event, "Done")]
        for source, event_type, message in events:
            for handler in self.handlers:
                handler(AgentEvent(source=source, event_type=event_type, message=message))
        return f"Result for {request}"


@pytest.fixture
def fake_coordinator(monkeypatch):
    import local_coder.orchestrator.coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "Coordinator", FakeCoordinator)
    return FakeCoordinator


def test_run_emits_result_session_and_events(tmp_path, fake_coordinator):
    result, doc = invoke("--project", str(tmp_path), "--json", "run", "add", "a", "test")
    assert result.exit_code == 0
    data = doc["data"]
    assert doc["command"] == "run"
    assert data["request"] == "add a test" and data["phase"] == "run"
    assert data["status"] == "completed"
    assert data["result"] == "Result for add a test"
    assert data["session_id"].startswith("local-")
    assert [e["event_type"] for e in data["events"]] == ["task_started", "task_completed"]
    # Human progress lines still go somewhere, just not stdout.
    assert "Processing: add a test" in result.stderr

    _, sessions = invoke("--project", str(tmp_path), "sessions", "--json")
    assert sessions["data"]["sessions"][0]["session_id"] == data["session_id"]


def test_run_with_model_error_is_not_ok(tmp_path, fake_coordinator, monkeypatch):
    monkeypatch.setattr(FakeCoordinator, "extra_events", [("CODER", "model_error", "connection refused")])
    result, doc = invoke("--project", str(tmp_path), "--json", "run", "x")
    assert result.exit_code == 1
    assert doc["ok"] is False
    assert doc["data"]["status"] == "completed"
    assert [e["message"] for e in doc["data"]["errors"]] == ["connection refused"]


def test_failed_verification_is_not_ok(tmp_path, fake_coordinator, monkeypatch):
    monkeypatch.setattr(FakeCoordinator, "final_event", "task_failed")
    result, doc = invoke("--project", str(tmp_path), "test", "--json")
    assert result.exit_code == 1
    assert doc["data"]["status"] == "failed" and doc["data"]["session_id"] is None


def test_interactive_mode_rejects_json(tmp_path):
    result, doc = invoke("--project", str(tmp_path), "--json")
    assert result.exit_code == 1
    assert doc["error"]["type"] == "UsageError"

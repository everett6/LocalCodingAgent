"""Tests for security tools, SecurityAuditorAgent, and AST taint flow analysis."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from local_coder.agents import create_agent
from local_coder.agents.security import SecurityAuditorAgent
from local_coder.cli.main import cli
from local_coder.intelligence.context_engine import MentionResolver
from local_coder.intelligence.indexer import CodeIndexer
from local_coder.tools import create_tool_registry
from local_coder.tools.security import AuditConfigTool, AuditDependenciesTool, RunSASTTool
from local_coder.types import (
    AgentRole,
    AgentTask,
    ModelResponse,
    ToolCall,
    ToolName,
)


@pytest.fixture
def temp_project(tmp_path: Path):
    """Create a temporary project structure with various code and config files."""
    app_py = tmp_path / "app.py"
    app_py.write_text(
        """
from flask import Flask, request
import os
import sqlite3
import pickle

app = Flask(__name__)

@app.route('/run')
def run_command():
    cmd = request.args.get('cmd')
    os.system(cmd)
    eval(cmd)
    return "ok"

@app.route('/query')
def query_db():
    name = request.args.get('name')
    conn = sqlite3.connect("test.db")
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM users WHERE name = '{name}'")
    data = pickle.loads(request.data)
    return "done"
""",
        encoding="utf-8",
    )

    clean_py = tmp_path / "clean.py"
    clean_py.write_text(
        """
def safe_add(a: int, b: int) -> int:
    return a + b
""",
        encoding="utf-8",
    )

    app_js = tmp_path / "client.js"
    app_js.write_text(
        """
function renderUser(name) {
    document.getElementById("user").innerHTML = name;
    eval(name);
}
""",
        encoding="utf-8",
    )

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        """
FROM python:latest
WORKDIR /app
COPY . .
CMD ["python", "app.py"]
""",
        encoding="utf-8",
    )

    env_file = tmp_path / ".env"
    env_file.write_text(
        """
DEBUG=True
DATABASE_URL=postgres://localhost:5432/db
SECRET_KEY='super_secret_token_1234567890abcdef'
""",
        encoding="utf-8",
    )

    reqs = tmp_path / "requirements.txt"
    reqs.write_text(
        """
flask==2.0.1
pyyaml<5.3
requests<2.19.0
""",
        encoding="utf-8",
    )

    return tmp_path


def run_async(coro):
    return asyncio.run(coro)


class TestSecurityTools:
    def test_run_sast_finds_vulnerabilities(self, temp_project: Path):
        tool = RunSASTTool(str(temp_project))
        result = run_async(tool.execute(target_path="app.py", tool="builtin"))
        assert result.success is True
        assert "SAST Security Report" in result.output
        assert "SEC-PY-OS-SYSTEM" in result.output
        assert "SEC-PY-CODE-EXEC" in result.output
        assert "SEC-PY-SQL-INJECTION" in result.output
        assert "SEC-PY-PICKLE-INSECURE" in result.output

    def test_run_sast_on_clean_code(self, temp_project: Path):
        tool = RunSASTTool(str(temp_project))
        result = run_async(tool.execute(target_path="clean.py", tool="builtin"))
        assert result.success is True
        assert "No matching static candidates" in result.output
        assert result.data["coverage"]["status"] == "complete"

    def test_run_sast_js_rules(self, temp_project: Path):
        tool = RunSASTTool(str(temp_project))
        result = run_async(tool.execute(target_path="client.js", tool="builtin"))
        assert result.success is True
        assert "SEC-JS-EVAL" in result.output
        assert "SEC-JS-INNERHTML-XSS" in result.output

    def test_audit_dependencies(self, temp_project: Path):
        tool = AuditDependenciesTool(str(temp_project))
        result = run_async(tool.execute(manifest_type="python"))
        assert result.success is True
        assert "Dependency Audit" in result.output
        assert "pyyaml" in result.output or "requests" in result.output

    def test_audit_config_and_secrets(self, temp_project: Path):
        tool = AuditConfigTool(str(temp_project))
        result = run_async(tool.execute(path="."))
        assert result.success is True
        assert "Config & Secret Audit" in result.output
        assert "DEBUG" in result.output or "secret" in result.output or "USER" in result.output


class TestSecurityTaintFlows:
    def test_indexer_finds_security_flows(self, temp_project: Path):
        indexer = CodeIndexer(languages=["python"])
        run_async(indexer.index_file(str(temp_project / "app.py")))
        flows = indexer.find_security_flows(str(temp_project / "app.py"))

        assert len(flows) >= 1
        fn_names = {f["function"] for f in flows}
        assert "run_command" in fn_names or "query_db" in fn_names

        summary = indexer.get_security_posture_summary()
        assert summary["detected_flows"] >= 1
        assert summary["critical_flows"] >= 1

    def test_mention_resolver_security(self, temp_project: Path):
        indexer = CodeIndexer(languages=["python"])
        run_async(indexer.index_file(str(temp_project / "app.py")))
        resolver = MentionResolver(indexer)

        resolved = run_async(resolver.resolve_mention("@security"))
        assert resolved is not None
        assert resolved["type"] == "security_summary"
        assert resolved["summary"]["detected_flows"] >= 1


class FakeSecurityModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.config = SimpleNamespace(temperature=0.0, max_tokens=200)

    async def generate(self, messages, **kwargs):
        return next(self.responses)


class TestSecurityAgent:
    def test_create_security_agent(self, temp_project: Path):
        registry = create_tool_registry(str(temp_project))
        model = FakeSecurityModel([])
        agent = create_agent(AgentRole.SECURITY, model, registry)
        assert isinstance(agent, SecurityAuditorAgent)
        assert agent.role == AgentRole.SECURITY
        assert "Security Researcher" in agent.system_prompt
        assert registry.has_permission(AgentRole.SECURITY, ToolName.RUN_SAST)
        assert registry.has_permission(AgentRole.SECURITY, ToolName.AUDIT_DEPENDENCIES)
        assert registry.has_permission(AgentRole.SECURITY, ToolName.AUDIT_CONFIG)

    def test_security_agent_loop(self, temp_project: Path):
        registry = create_tool_registry(str(temp_project))
        model = FakeSecurityModel([
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call_sast", name="run_sast", arguments={"target_path": "clean.py"}),
                ],
            ),
            ModelResponse(content="Security audit finished: No vulnerabilities detected in clean.py.", tool_calls=[]),
        ])

        agent = create_agent(AgentRole.SECURITY, model, registry)
        task = AgentTask(role=AgentRole.SECURITY, objective="Audit clean.py")
        response = run_async(agent.execute(task))

        assert response.status.value == "completed"
        assert "Security audit finished" in response.summary


class TestSecurityCli:
    def test_audit_cli_command(self, temp_project: Path):
        runner = CliRunner()
        with patch("local_coder.cli.main._run_audit") as mock_audit:
            result = runner.invoke(cli, ["audit", "--target", "app.py"])
            assert result.exit_code == 0
            mock_audit.assert_called_once()

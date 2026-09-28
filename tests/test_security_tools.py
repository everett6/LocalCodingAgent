"""Tests for the security_scan tool and the security review agent."""
import asyncio
import json
import stat
from types import SimpleNamespace

import pytest

from local_coder.agents import SecurityAgent, create_agent
from local_coder.tools import create_tool_registry
from local_coder.tools.security import SecurityScanTool, parse_bandit, parse_semgrep, scan_secrets
from local_coder.types import AgentRole, ToolName

# Assembled at runtime so this file never contains a credential-shaped literal.
AWS_KEY = "AKIA" + "Q" * 16
GITHUB_TOKEN = "ghp_" + "a1" * 18


def _fake_executable(bin_dir, name, script):
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_scan_secrets_finds_and_redacts(project):
    (project / "settings.py").write_text(
        f'AWS_KEY = "{AWS_KEY}"\n'
        'DB_PASSWORD = "s3cr3t-hunter2"\n'
        'API_KEY = "your-api-key-here"\n'
    )
    (project / "deploy").mkdir()
    (project / "deploy" / "id_rsa").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n")
    (project / "notes.md").write_text(f"token: {GITHUB_TOKEN}\n")

    findings = scan_secrets(project, [project])
    rendered = "\n".join(f.render() for f in findings)

    assert [(f.path, f.line, f.rule) for f in findings] == [
        ("deploy/id_rsa", 1, "private-key"),
        ("notes.md", 1, "github-token"),
        ("settings.py", 1, "aws-access-key"),
        ("settings.py", 2, "hardcoded-credential"),
    ]
    assert AWS_KEY not in rendered and GITHUB_TOKEN not in rendered
    assert "s3cr3t-hunter2" not in rendered
    assert "(20 chars)" in rendered


def test_scan_secrets_skips_binary_and_excluded_dirs(project):
    (project / "blob.bin").write_bytes(b"\0" + AWS_KEY.encode())
    (project / "node_modules").mkdir()
    (project / "node_modules" / "dep.js").write_text(f'k = "{AWS_KEY}"\n')

    assert scan_secrets(project, [project]) == []


def test_parse_bandit_and_semgrep():
    bandit = json.dumps({"results": [{
        "filename": "./app.py", "line_number": 7, "issue_severity": "HIGH",
        "issue_confidence": "HIGH", "test_id": "B602", "issue_text": "subprocess call with shell=True",
    }]})
    semgrep = json.dumps({"results": [{
        "path": "api/views.py", "start": {"line": 12}, "check_id": "sql-injection",
        "extra": {"severity": "ERROR", "message": "User input reaches a raw SQL query\nmore"},
    }]})

    assert parse_bandit(bandit)[0].render() == (
        "app.py:7 [HIGH] bandit/B602: subprocess call with shell=True (confidence high)"
    )
    assert parse_semgrep(semgrep)[0].render() == (
        "api/views.py:12 [HIGH] semgrep/sql-injection: User input reaches a raw SQL query"
    )


def test_security_scan_combines_scanners_and_sorts_by_severity(project, fake_bin):
    (project / "app.py").write_text('password = "correct-horse-battery"\n')
    report = json.dumps({"results": [{
        "filename": "./app.py", "line_number": 3, "issue_severity": "HIGH",
        "issue_confidence": "MEDIUM", "test_id": "B307", "issue_text": "Use of eval",
    }]})
    (fake_bin / "report.json").write_text(report)
    _fake_executable(fake_bin, "bandit", f'/bin/cat "{fake_bin}/report.json"; exit 1\n')

    result = asyncio.run(SecurityScanTool(str(project)).execute())

    assert result.success, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Scanners run: secrets, bandit"
    assert lines[1] == "Findings: 2 (1 high, 1 medium)"
    assert lines[2].startswith("app.py:3 [HIGH] bandit/B307")
    assert lines[3].startswith("app.py:1 [MEDIUM] secrets/hardcoded-credential")
    assert "semgrep is not installed" in result.output


def test_semgrep_only_runs_with_local_rules(project, fake_bin):
    _fake_executable(fake_bin, "semgrep", 'echo "{\\"results\\": []}"; echo "$@" > semgrep-args\n')

    skipped = asyncio.run(SecurityScanTool(str(project)).execute(scanners=["semgrep"]))
    assert not skipped.success
    assert "no local rules" in skipped.output

    (project / ".semgrep.yml").write_text("rules: []\n")
    ran = asyncio.run(SecurityScanTool(str(project)).execute(scanners=["semgrep"]))
    assert ran.success, ran.output
    assert (project / "semgrep-args").read_text().strip() == (
        "scan --config .semgrep.yml --json --metrics=off --quiet ."
    )


def test_security_scan_reports_scanner_errors(project, fake_bin):
    _fake_executable(fake_bin, "bandit", 'echo "boom" >&2; exit 2\n')

    result = asyncio.run(SecurityScanTool(str(project)).execute(scanners=["bandit"]))

    assert not result.success
    assert "bandit failed: boom" in result.output


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"scanners": ["nmap"]}, "Unknown scanner(s): nmap"),
        ({"paths": ["../outside"]}, "escapes workspace"),
    ],
)
def test_security_scan_rejects_bad_requests(project, kwargs, message):
    result = asyncio.run(SecurityScanTool(str(project)).execute(**kwargs))

    assert not result.success
    assert message in result.output


def test_security_role_is_read_only(tmp_path):
    registry = create_tool_registry(str(tmp_path))
    allowed = {tool.name for tool in registry.get_tools_for_role(AgentRole.SECURITY)}

    assert ToolName.SECURITY_SCAN in allowed and ToolName.READ_FILE in allowed
    assert allowed.isdisjoint({
        ToolName.WRITE_FILE, ToolName.EDIT_FILE, ToolName.APPLY_PATCH, ToolName.FORMAT_CODE,
        ToolName.RUN_COMMAND, ToolName.GIT_COMMIT, ToolName.GIT_CHECKOUT,
    })


def test_create_agent_builds_security_agent(tmp_path):
    model = SimpleNamespace(config=SimpleNamespace(temperature=0.0, max_tokens=100))
    agent = create_agent(AgentRole.SECURITY, model, create_tool_registry(str(tmp_path)))

    assert isinstance(agent, SecurityAgent)
    assert "red team" in agent.system_prompt and "blue team" in agent.system_prompt

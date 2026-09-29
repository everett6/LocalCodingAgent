import asyncio
import json
import sys

import pytest
import yaml
from click.testing import CliRunner

from local_coder import hooks
from local_coder.cli import main as cli_main
from local_coder.cli.main import cli
from local_coder.tools import create_tool_registry
from local_coder.types import AgentRole

PY = f'"{sys.executable}"'


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_HOME", str(tmp_path / "home"))


def _config(tmp_path, text):
    path = tmp_path / ".local-coder" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- parsing -----------------------------------------------------------------

def test_parse_hooks_accepts_strings_mappings_and_lists():
    parsed, errors = hooks.parse_hooks({
        "after_edit": "echo edited",
        "after_tests": {"command": "echo tests", "timeout": 5},
        "before_tool": [{"command": "echo x", "tools": "run_command"}, "echo y"],
    })
    assert errors == []
    assert [(h.event, h.command) for h in parsed] == [
        ("after_edit", "echo edited"), ("after_tests", "echo tests"),
        ("before_tool", "echo x"), ("before_tool", "echo y"),
    ]
    assert parsed[1].timeout == 5
    assert parsed[2].tools == frozenset({"run_command"})
    assert parsed[3].tools == frozenset()


def test_parse_hooks_reports_bad_entries():
    parsed, errors = hooks.parse_hooks({
        "on_lunch": "echo",
        "after_edit": [{"command": ""}, {"command": "ok", "timeout": "soon"}, {"command": "ok", "tools": 3}, 7],
        "after_tests": "echo fine",
    })
    assert [h.command for h in parsed] == ["echo fine"]
    assert len(errors) == 5
    assert hooks.parse_hooks(["not", "a", "mapping"])[1]


def test_load_hooks_uses_config_search_order(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("hooks:\n  after_edit: echo fallback\n")
    assert hooks.load_hooks(None, tmp_path).hooks[0].command == "echo fallback"

    _config(tmp_path, "hooks:\n  after_edit: echo primary\n")
    assert hooks.load_hooks(None, tmp_path).hooks[0].command == "echo primary"

    explicit = tmp_path / "other.yaml"
    explicit.write_text("hooks:\n  after_edit: echo explicit\n")
    loaded = hooks.load_hooks(str(explicit), tmp_path)
    assert loaded.hooks[0].command == "echo explicit" and loaded.source == explicit


def test_load_hooks_without_hooks_section(tmp_path):
    _config(tmp_path, "models: {}\n")
    config = hooks.load_hooks(None, tmp_path)
    assert config.hooks == [] and config.source is not None
    assert hooks.load_hooks(None, tmp_path / "missing").hooks == []


# --- trust -------------------------------------------------------------------

def test_trust_is_per_project_and_resets_when_hooks_change(tmp_path):
    _config(tmp_path, "hooks:\n  after_edit: echo one\n")
    config = hooks.load_hooks(None, tmp_path)
    assert not hooks.is_trusted(config, tmp_path)

    hooks.trust(config, tmp_path)
    assert hooks.is_trusted(config, tmp_path)
    assert not hooks.is_trusted(config, tmp_path / "elsewhere")

    _config(tmp_path, "hooks:\n  after_edit: echo two\n")
    assert not hooks.is_trusted(hooks.load_hooks(None, tmp_path), tmp_path)

    assert hooks.untrust(tmp_path) is True
    assert hooks.untrust(tmp_path) is False
    assert not hooks.is_trusted(config, tmp_path)


def test_no_hooks_counts_as_trusted(tmp_path):
    assert hooks.is_trusted(hooks.HookConfig(), tmp_path)


# --- running through the tool registry ---------------------------------------

def _registry_with(tmp_path, hook_section):
    _config(tmp_path, yaml.safe_dump({"hooks": hook_section}))
    registry = create_tool_registry(str(tmp_path))
    hooks.install(registry, hooks.HookRunner(hooks.load_hooks(None, tmp_path), tmp_path))
    return registry


def _run(coro):
    return asyncio.run(coro)


def test_after_edit_hook_gets_env_and_stdin(tmp_path):
    script = tmp_path / "record.py"
    script.write_text(
        "import json, os, sys\n"
        "payload = json.load(sys.stdin)\n"
        "with open('hook.log', 'a') as f:\n"
        "    f.write(json.dumps({'event': os.environ['LOCAL_CODER_EVENT'], 'tool': os.environ['LOCAL_CODER_TOOL'],"
        " 'file': os.environ['LOCAL_CODER_FILE'], 'success': os.environ['LOCAL_CODER_SUCCESS'],"
        " 'payload_tool': payload['tool'], 'has_output': 'output' in payload}) + '\\n')\n"
    )
    registry = _registry_with(tmp_path, {"after_edit": f"{PY} record.py", "after_tests": f"{PY} record.py"})

    result = _run(registry.execute_tool(AgentRole.CODER, "write_file", {"path": "a.txt", "content": "hi"}))
    assert result.success
    assert (tmp_path / "a.txt").read_text() == "hi"
    # Read-only tools don't trigger edit hooks.
    _run(registry.execute_tool(AgentRole.CODER, "read_file", {"path": "a.txt"}))

    lines = [json.loads(l) for l in (tmp_path / "hook.log").read_text().splitlines()]
    assert lines == [{"event": "after_edit", "tool": "write_file", "file": "a.txt", "success": "1",
                      "payload_tool": "write_file", "has_output": True}]


def test_failing_before_hook_blocks_the_call(tmp_path):
    registry = _registry_with(
        tmp_path,
        {"before_edit": [{"command": "echo 'no edits to secrets' && exit 3"}]},
    )
    result = _run(registry.execute_tool(AgentRole.CODER, "write_file", {"path": "secret.txt", "content": "x"}))

    assert not result.success
    assert "Blocked by a hook" in result.output and "no edits to secrets" in result.output
    assert "exited 3" in result.output
    assert not (tmp_path / "secret.txt").exists()


def test_tools_filter_limits_which_calls_run_the_hook(tmp_path):
    registry = _registry_with(
        tmp_path,
        {"before_tool": [{"tools": ["run_tests"], "command": "exit 1"}]},
    )
    result = _run(registry.execute_tool(AgentRole.CODER, "write_file", {"path": "a.txt", "content": "x"}))
    assert result.success


def test_failing_after_hook_output_is_appended_but_success_kept(tmp_path):
    registry = _registry_with(tmp_path, {"after_edit": "echo 'lint: 2 problems' && exit 1"})
    result = _run(registry.execute_tool(AgentRole.CODER, "write_file", {"path": "a.txt", "content": "x"}))

    assert result.success
    assert "lint: 2 problems" in result.output and "after_edit hook" in result.output


def test_passing_after_hook_leaves_output_alone(tmp_path):
    registry = _registry_with(tmp_path, {"after_tool": "echo noisy"})
    result = _run(registry.execute_tool(AgentRole.CODER, "write_file", {"path": "a.txt", "content": "x"}))
    assert "noisy" not in result.output


def test_hook_timeout_is_reported(tmp_path):
    registry = _registry_with(
        tmp_path,
        {"before_edit": [{"command": f'{PY} -c "import time; time.sleep(30)"', "timeout": 1}]},
    )
    result = _run(registry.execute_tool(AgentRole.CODER, "write_file", {"path": "a.txt", "content": "x"}))
    assert not result.success and "timed out after 1s" in result.output


def test_hooks_skip_calls_the_registry_rejects(tmp_path):
    registry = _registry_with(tmp_path, {"before_tool": "echo ran >> hook.log"})
    # Reviewer may not write files; unknown tools are rejected too.
    _run(registry.execute_tool(AgentRole.REVIEWER, "write_file", {"path": "a.txt", "content": "x"}))
    _run(registry.execute_tool(AgentRole.CODER, "no_such_tool", {}))
    assert not (tmp_path / "hook.log").exists()


def test_install_is_noop_without_hooks(tmp_path):
    registry = create_tool_registry(str(tmp_path))
    original = registry.execute_tool
    hooks.install(registry, hooks.HookRunner(hooks.HookConfig(), tmp_path))
    assert registry.execute_tool == original


# --- CLI -----------------------------------------------------------------------

def test_untrusted_hooks_are_not_installed(tmp_path, monkeypatch):
    _config(tmp_path, "hooks:\n  after_edit: echo hi\n")

    class FakeCoordinator:
        tool_registry = create_tool_registry(str(tmp_path))

    coordinator = FakeCoordinator()
    ctx_obj = {"project_root": str(tmp_path), "config_path": None}
    cli_main._install_hooks(coordinator, ctx_obj)
    assert not getattr(coordinator.tool_registry, "_hooks_installed", False)
    assert ctx_obj["_warned_untrusted_hooks"]

    hooks.trust(hooks.load_hooks(None, tmp_path), tmp_path)
    cli_main._install_hooks(coordinator, ctx_obj)
    assert coordinator.tool_registry._hooks_installed


def test_hooks_cli_show_trust_untrust(tmp_path):
    _config(tmp_path, "hooks:\n  after_edit: echo hi\n")
    runner = CliRunner()

    shown = runner.invoke(cli, ["--project", str(tmp_path), "hooks"])
    assert shown.exit_code == 0 and "not trusted" in shown.output and "echo hi" in shown.output

    declined = runner.invoke(cli, ["--project", str(tmp_path), "hooks", "trust"], input="n\n")
    assert declined.exit_code == 0
    assert not hooks.is_trusted(hooks.load_hooks(None, tmp_path), tmp_path)

    trusted = runner.invoke(cli, ["--project", str(tmp_path), "hooks", "trust", "--yes"])
    assert trusted.exit_code == 0 and "Hooks trusted" in trusted.output
    assert hooks.is_trusted(hooks.load_hooks(None, tmp_path), tmp_path)

    untrusted = runner.invoke(cli, ["--project", str(tmp_path), "hooks", "untrust"])
    assert untrusted.exit_code == 0 and "no longer trusted" in untrusted.output


def test_hooks_cli_without_hooks(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "hooks"])
    assert result.exit_code == 0 and "No hooks configured" in result.output


def test_interactive_setup_offers_to_trust_hooks(tmp_path, monkeypatch):
    _config(tmp_path, "hooks:\n  after_edit: echo hi\n")
    ctx_obj = {"project_root": str(tmp_path), "config_path": None}

    monkeypatch.setattr(cli_main.click, "confirm", lambda *a, **k: False)
    cli_main._setup_interactive_extensions(ctx_obj)
    assert not hooks.is_trusted(hooks.load_hooks(None, tmp_path), tmp_path)

    monkeypatch.setattr(cli_main.click, "confirm", lambda *a, **k: True)
    cli_main._setup_interactive_extensions(ctx_obj)
    assert hooks.is_trusted(hooks.load_hooks(None, tmp_path), tmp_path)

    # Already trusted: no prompt at all.
    def fail(*a, **k):
        raise AssertionError("prompted again")
    monkeypatch.setattr(cli_main.click, "confirm", fail)
    cli_main._setup_interactive_extensions(ctx_obj)

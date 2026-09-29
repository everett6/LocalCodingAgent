from click.testing import CliRunner

from local_coder.cli.main import cli


def test_version_is_available():
    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert "local-coder, version" in result.output


def test_init_creates_project_configuration(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "init"])

    assert result.exit_code == 0
    config_path = tmp_path / ".local-coder" / "config.yaml"
    assert config_path.is_file()
    assert "model_id: your-model-name" in config_path.read_text()


def test_init_does_not_overwrite_existing_configuration(tmp_path):
    config_path = tmp_path / ".local-coder" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("existing")

    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "init"])

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert config_path.read_text() == "existing"


def test_init_writes_safe_approval_defaults(tmp_path):
    """local-coder init used to write require_approval_for_*: false into
    the generated config, silently disabling all approval prompts for a
    project that had never touched the setting."""
    CliRunner().invoke(cli, ["--project", str(tmp_path), "init"])

    config_path = tmp_path / ".local-coder" / "config.yaml"
    text = config_path.read_text()
    assert "require_approval_for_commands: true" in text
    assert "require_approval_for_commits: true" in text


def test_agents_command_does_not_crash(tmp_path):
    """Regression test: `local-coder agents` used to raise AttributeError
    because it read config.agents, a field ProjectConfig never had."""
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "agents"])

    assert result.exit_code == 0
    assert "coder" in result.output
    assert "planner" in result.output


def test_models_command_does_not_crash(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "models"])

    assert result.exit_code == 0


def test_status_command_does_not_crash(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "status"])

    assert result.exit_code == 0


def test_sessions_command_does_not_crash(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "sessions"])

    assert result.exit_code == 0


def test_yolo_flag_is_accepted(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "--yolo", "agents"])

    assert result.exit_code == 0


def test_local_server_status_command_does_not_crash(tmp_path, monkeypatch):
    from local_coder import local_server
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)

    result = CliRunner().invoke(cli, ["local-server", "status"])

    assert result.exit_code == 0
    assert "big" in result.output
    assert "draft" in result.output


def test_local_server_models_command_does_not_crash(tmp_path, monkeypatch):
    from local_coder import local_server
    monkeypatch.setattr(local_server, "AI2_DIR", str(tmp_path))

    result = CliRunner().invoke(cli, ["local-server", "models"])

    assert result.exit_code == 0

def test_config_command_shows_routing_and_hides_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("TEST_LC_CLI_KEY", "sk-do-not-print")
    monkeypatch.setenv("COLUMNS", "200")
    config_path = tmp_path / ".local-coder" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        "models:\n"
        "  fast: {model_id: small, api_key_env: TEST_LC_CLI_KEY}\n"
        "  strong: {model_id: big}\n"
        "routing:\n"
        "  roles: {planner: strong}\n"
        "  escalate_to: strong\n"
    )

    result = CliRunner().invoke(
        cli, ["--project", str(tmp_path), "--set", "tools.command_timeout=90", "config"]
    )

    assert result.exit_code == 0, result.output
    assert "sk-do-not-print" not in result.output
    assert "$TEST_LC_CLI_KEY (set)" in result.output
    assert "Escalate failed steps to: strong" in result.output
    assert "command_timeout=90" in result.output
    assert "No problems found" in result.output


def test_config_errors_are_reported_without_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(tmp_path / "none.yaml"))
    config_path = tmp_path / ".local-coder" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("models:\n  m: {model_id: x, api_key: sk-plain}\n")

    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "config"])

    assert result.exit_code != 0
    assert "plaintext key" in result.output

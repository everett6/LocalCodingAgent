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
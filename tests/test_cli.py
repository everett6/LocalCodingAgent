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
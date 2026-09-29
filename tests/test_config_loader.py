"""Tests for config_loader.load_config -- specifically that every value it
reads from YAML actually lands on a real field of the pydantic config
models. Pydantic silently drops unknown kwargs by default, so a mismatch
between the keys load_config passes and the fields the target model
declares fails silently (no error, the value is just ignored) instead of
crashing -- these tests catch that class of bug directly, which is how a
previous version of this file passed nonexistent require_gpu/test_command
kwargs and require_approval_for_commands/commits under the wrong field
names without any test noticing.
"""
from local_coder.orchestrator.config_loader import load_config


def write_config(tmp_path, text: str):
    config_dir = tmp_path / ".local-coder"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(text, encoding="utf-8")


def test_approval_settings_are_honored_from_yaml(tmp_path):
    write_config(tmp_path, """
approval:
  require_approval_for_commands: false
  require_approval_for_commits: true
""")

    config = load_config(project_root=str(tmp_path))

    assert config.approval.require_approval_for_commands is False
    assert config.approval.require_approval_for_commits is True


def test_approval_defaults_to_safe_when_section_is_absent(tmp_path):
    write_config(tmp_path, "models: {}\n")

    config = load_config(project_root=str(tmp_path))

    assert config.approval.require_approval_for_commands is True
    assert config.approval.require_approval_for_commits is True


def test_resource_and_verification_settings_are_honored_from_yaml(tmp_path):
    write_config(tmp_path, """
resources:
  max_concurrent_gpu_agents: 2
  max_concurrent_cpu_agents: 6
verification:
  run_tests_after_changes: false
  max_fix_iterations: 7
""")

    config = load_config(project_root=str(tmp_path))

    assert config.resources.max_concurrent_gpu_agents == 2
    assert config.resources.max_concurrent_cpu_agents == 6
    assert config.verification.run_tests_after_changes is False
    assert config.verification.max_fix_iterations == 7


def test_agentic_settings_are_honored_from_yaml(tmp_path):
    write_config(tmp_path, """
agentic:
  role_models:
    coder: fast-model
  context_window_chars: 1000
  compact_context_chars: 500
  max_parallel_agents: 3
""")

    config = load_config(project_root=str(tmp_path))

    assert config.agentic.role_models == {"coder": "fast-model"}
    assert config.agentic.context_window_chars == 1000
    assert config.agentic.compact_context_chars == 500
    assert config.agentic.max_parallel_agents == 3


def test_load_config_falls_back_to_defaults_with_no_file(tmp_path):
    config = load_config(project_root=str(tmp_path))

    assert config.models == {}
    assert config.approval.require_approval_for_commands is True


# --- Layered config, env-var API keys, --set overrides, tools, routing ---

import pytest

from local_coder.orchestrator.config_loader import ConfigError, validate_config


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    """Point the per-user config at a file inside tmp_path so a developer's
    own ~/.config/local-coder/config.yaml can't leak into these tests."""
    path = tmp_path / "user-config.yaml"
    monkeypatch.setenv("LOCAL_CODER_USER_CONFIG", str(path))
    return path


def test_project_config_merges_over_user_config(tmp_path, isolated_user_config):
    isolated_user_config.write_text("""
models:
  strong:
    model_id: big
    base_url: http://gpu-box:8090/v1
    temperature: 0.3
verification:
  max_fix_iterations: 9
""")
    write_config(tmp_path, """
models:
  strong:
    temperature: 0.1
""")

    config = load_config(project_root=str(tmp_path))

    assert config.models["strong"].model_id == "big"
    assert config.models["strong"].base_url == "http://gpu-box:8090/v1"
    assert config.models["strong"].temperature == 0.1
    assert config.verification.max_fix_iterations == 9
    assert config.sources == [str(isolated_user_config), str(tmp_path / ".local-coder" / "config.yaml")]


def test_root_level_dotfile_is_found(tmp_path):
    (tmp_path / ".local-coder.yaml").write_text("verification:\n  max_fix_iterations: 4\n")

    assert load_config(project_root=str(tmp_path)).verification.max_fix_iterations == 4


def test_model_defaults_apply_to_every_model(tmp_path):
    write_config(tmp_path, """
model_defaults:
  backend: openai_compatible
  base_url: http://localhost:9000/v1
models:
  a: {model_id: one}
  b: {model_id: two, base_url: http://other/v1}
""")

    config = load_config(project_root=str(tmp_path))

    assert config.models["a"].base_url == "http://localhost:9000/v1"
    assert config.models["a"].backend.value == "openai_compatible"
    assert config.models["b"].base_url == "http://other/v1"


def test_api_key_is_read_from_named_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_LC_KEY", "sk-secret")
    write_config(tmp_path, """
models:
  hosted: {model_id: m, api_key_env: TEST_LC_KEY}
  braced: {model_id: m, api_key: "${TEST_LC_KEY}"}
""")

    config = load_config(project_root=str(tmp_path))

    assert config.models["hosted"].api_key == "sk-secret"
    assert config.models["braced"].api_key == "sk-secret"
    assert config.models["braced"].api_key_env == "TEST_LC_KEY"
    # Never serialized or shown in repr.
    assert "sk-secret" not in repr(config)
    assert "sk-secret" not in config.model_dump_json()


def test_plaintext_api_key_in_file_is_refused(tmp_path, isolated_user_config):
    isolated_user_config.write_text("models:\n  m: {model_id: x, api_key: sk-live-123}\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(project_root=str(tmp_path))

    assert "api_key_env" in str(excinfo.value)
    assert str(isolated_user_config) in str(excinfo.value)


def test_missing_env_var_is_reported_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_LC_MISSING", raising=False)
    write_config(tmp_path, "models:\n  m: {model_id: x, api_key_env: TEST_LC_MISSING}\n")

    config = load_config(project_root=str(tmp_path))

    assert config.models["m"].api_key is None
    assert any("TEST_LC_MISSING" in p for p in validate_config(config))


def test_set_overrides_and_temperature_win_over_files(tmp_path):
    write_config(tmp_path, """
models:
  coder: {model_id: x, temperature: 0.2}
verification:
  max_fix_iterations: 3
""")

    config = load_config(
        project_root=str(tmp_path),
        overrides=["verification.max_fix_iterations=5", "tools.command_timeout=300", "routing.roles.coder=coder"],
    )
    assert config.verification.max_fix_iterations == 5
    assert config.tools.command_timeout == 300
    assert config.agentic.role_models["coder"] == "coder"
    assert config.sources[-1] == "--set"

    assert load_config(project_root=str(tmp_path), temperature=0.9).models["coder"].temperature == 0.9


def test_set_rejects_api_keys_and_malformed_items(tmp_path):
    with pytest.raises(ConfigError):
        load_config(project_root=str(tmp_path), overrides=["models.m.api_key=sk-1"])
    with pytest.raises(ConfigError):
        load_config(project_root=str(tmp_path), overrides=["no-equals-sign"])


def test_invalid_yaml_and_missing_explicit_path_raise(tmp_path):
    write_config(tmp_path, "models: [unclosed\n")
    with pytest.raises(ConfigError):
        load_config(project_root=str(tmp_path))
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"), project_root=str(tmp_path))


def test_routing_section(tmp_path):
    write_config(tmp_path, """
models:
  fast: {model_id: small}
  strong: {model_id: big}
agentic:
  role_models: {coder: fast, reviewer: fast}
routing:
  roles: {coder: strong}
  escalate_to: strong
  fallbacks:
    strong: fast
""")

    config = load_config(project_root=str(tmp_path))

    assert config.agentic.role_models == {"coder": "strong", "reviewer": "fast"}
    assert config.routing.escalate_to == "strong"
    assert config.routing.fallbacks == {"strong": ["fast"]}
    assert validate_config(config) == []


def test_validate_flags_unknown_models_and_tools(tmp_path):
    write_config(tmp_path, """
models:
  fast: {model_id: small}
routing:
  roles: {coder: missing}
  escalate_to: also-missing
tools:
  disabled: [not_a_tool]
""")

    problems = validate_config(load_config(project_root=str(tmp_path)))

    assert any("missing" in p and "routing.roles.coder" in p for p in problems)
    assert any("escalate_to" in p for p in problems)
    assert any("not_a_tool" in p for p in problems)


def test_tools_section_and_legacy_permissions(tmp_path):
    write_config(tmp_path, """
tools:
  disabled: [git_commit]
  command_timeout: 120
  roles:
    reviewer: [read_file, grep]
permissions:
  - role: tester
    allowed_tools: [read_file, run_tests]
""")

    config = load_config(project_root=str(tmp_path))

    assert config.tools.disabled == ["git_commit"]
    assert config.tools.command_timeout == 120
    assert config.tools.roles == {"reviewer": ["read_file", "grep"], "tester": ["read_file", "run_tests"]}

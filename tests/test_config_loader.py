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

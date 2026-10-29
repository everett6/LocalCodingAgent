"""Regression coverage for the legacy configuration entry point."""
import importlib

import pytest


def legacy():
    return importlib.import_module("local_coder.core.config")


def test_legacy_module_import_and_project_defaults():
    assert legacy().ProjectConfig().log_level == "INFO"


def test_json_schema_defaults_merge_recursively_without_mutating_inputs():
    module = legacy()
    schema = {"type": "object", "properties": {
        "logging": {"type": "object", "properties": {
            "level": {"type": "string", "default": "INFO"},
            "enabled": {"type": "boolean", "default": False},
            "optional": {"type": ["string", "null"], "default": None},
        }},
    }, "additionalProperties": True}
    defaults = {"logging": {"level": "WARNING"}}
    supplied = {"logging": {"enabled": True}, "extension": {"custom": 1}}
    result = module.ConfigSchema(schema, defaults).validate(supplied)
    assert result == {"logging": {"level": "WARNING", "enabled": True, "optional": None},
                      "extension": {"custom": 1}}
    assert defaults == {"logging": {"level": "WARNING"}}
    assert supplied == {"logging": {"enabled": True}, "extension": {"custom": 1}}


@pytest.mark.parametrize("config", [{"count": "wrong"}, {"unknown": 1}, [1]])
def test_json_schema_preserves_type_and_unknown_field_validation(config):
    module = legacy()
    schema = {"type": "object", "properties": {"count": {"type": "integer", "default": 1}},
              "additionalProperties": False}
    with pytest.raises(module.ConfigError, match="validation failed"):
        module.ConfigSchema(schema).validate(config)


def test_legacy_manager_loads_yaml_defaults_and_safe_nested_access(tmp_path):
    module = legacy()
    target = tmp_path / "config.yaml"
    target.write_text("resources:\n  workers: 2\ncustom: enabled\n")
    manager = module.ConfigManager(str(target))
    result = manager.load()
    assert result.log_level == "INFO"
    assert result.custom == "enabled"
    assert manager.get("resources.workers") == 2
    assert manager.get("resources.missing.child", "fallback") == "fallback"

import copy
import os
import re
import yaml
from pathlib import Path
from typing import Any, Iterable, Optional

from local_coder.types import (
    ProjectConfig, ModelConfig, ModelBackend, ResourceConfig, VerificationConfig, ApprovalConfig,
    AgenticConfig, RoutingConfig, ToolSettings,
)


class ConfigError(ValueError):
    """A config file is present but unusable (bad YAML, a plaintext secret, a bad --set)."""


_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def user_config_path() -> Path:
    """The per-user config file, merged underneath the project's own config.

    LOCAL_CODER_USER_CONFIG overrides the location (tests point it at a
    nonexistent file so a developer's own config can't leak into them).
    """
    override = os.environ.get("LOCAL_CODER_USER_CONFIG")
    if override is not None:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "local-coder" / "config.yaml"


def project_config_paths(project_root: str) -> list[Path]:
    """Project config locations in priority order; the first that exists wins."""
    root = Path(project_root)
    return [
        root / ".local-coder" / "config.yaml",
        root / ".local-coder.yaml",
        root / "config" / "config.yaml",
    ]


def _read_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    _reject_plaintext_keys(data, str(path))
    return data


def _reject_plaintext_keys(data: dict, source: str) -> None:
    """API keys may only be referenced from the environment, never written into a config file."""
    blocks = [("model_defaults", data.get("model_defaults") or {})]
    blocks += [(f"models.{name}", m or {}) for name, m in _normalize_models(data.get("models")).items()]
    for label, block in blocks:
        literal = block.get("api_key") if isinstance(block, dict) else None
        if literal is not None and not _ENV_REF.match(str(literal).strip()):
            raise ConfigError(
                f"{label}.api_key in {source} looks like a plaintext key. "
                f"Put the key in an environment variable and reference it with "
                f"`api_key_env: VAR_NAME` (or `api_key: \"${{VAR_NAME}}\"`)."
            )


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override onto base: nested mappings merge, anything else is replaced."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _normalize_models(models_data: Any) -> dict:
    # Accept the keyed mapping used by the project config and legacy lists.
    if isinstance(models_data, list):
        return {m.get("name", "model"): m for m in models_data}
    return dict(models_data or {})


def apply_overrides(data: dict, overrides: Iterable[str]) -> dict:
    """Apply `dotted.key=value` overrides (the CLI's --set). Values are parsed as YAML,
    so `--set verification.max_fix_iterations=5` stores an int and `=false` a bool."""
    data = copy.deepcopy(data)
    for item in overrides:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigError(f"--set expects key=value, got {item!r}")
        try:
            value = yaml.safe_load(raw) if raw.strip() else ""
        except yaml.YAMLError:
            value = raw
        parts = key.split(".")
        if parts[-1] == "api_key":
            raise ConfigError("Refusing to take an API key on the command line; set api_key_env instead")
        node = data
        for part in parts[:-1]:
            if part == "models":
                node["models"] = _normalize_models(node.get("models"))
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
    return data


def _resolve_api_key(model_data: dict) -> tuple[str | None, str | None]:
    """Return (api_key, env_var_name). Files were already checked for plaintext keys."""
    env_name = model_data.get("api_key_env")
    match = _ENV_REF.match(str(model_data.get("api_key") or "").strip())
    if match:
        env_name = env_name or match.group(1)
    if not env_name:
        return None, None
    return os.environ.get(env_name) or None, env_name


def load_config(
    config_path: Optional[str] = None,
    project_root: str = ".",
    overrides: Iterable[str] = (),
    temperature: Optional[float] = None,
) -> ProjectConfig:
    """Load configuration from YAML files plus command-line overrides.

    Layers, lowest priority first:
    1. The per-user config (~/.config/local-coder/config.yaml)
    2. The project config: an explicit --config path, else the first of
       .local-coder/config.yaml, .local-coder.yaml, config/config.yaml
    3. `overrides` (dotted key=value strings from --set)
    4. `temperature` (--temperature), applied to every model
    """
    sources: list[str] = []
    data: dict = {}

    user_path = user_config_path()
    if user_path.is_file():
        data = _read_yaml(user_path)
        sources.append(str(user_path))

    if config_path:
        explicit = Path(config_path)
        if not explicit.is_file():
            raise ConfigError(f"Config file not found: {config_path}")
        project_paths = [explicit]
    else:
        project_paths = project_config_paths(project_root)
    for path in project_paths:
        if path.is_file():
            project_data = _read_yaml(path)
            # Merge models per name so a project can adjust one field of a
            # model the user config defines without restating the endpoint.
            if "models" in data or "models" in project_data:
                data["models"] = _normalize_models(data.get("models"))
                project_data = dict(project_data, models=_normalize_models(project_data.get("models")))
            data = deep_merge(data, project_data)
            sources.append(str(path))
            break

    overrides = list(overrides)
    if overrides:
        data = apply_overrides(data, overrides)
        sources.append("--set")

    defaults = data.get("model_defaults") or {}
    models = {}
    for name, raw_model in _normalize_models(data.get("models")).items():
        model_data = deep_merge(defaults, raw_model or {})
        backend_value = model_data.get("backend", ModelBackend.OLLAMA.value)
        try:
            backend = ModelBackend(backend_value)
        except ValueError:
            backend = ModelBackend.OLLAMA
        api_key, api_key_env = _resolve_api_key(model_data)
        models[name] = ModelConfig(
            model_id=model_data.get("model_id", "default"),
            name=model_data.get("name", name),
            backend=backend,
            base_url=model_data.get("base_url", "http://localhost:11434"),
            context_length=model_data.get("context_length", 8192),
            temperature=temperature if temperature is not None else model_data.get("temperature", 0.2),
            max_tokens=model_data.get("max_tokens", 4096),
            quantization=model_data.get("quantization"),
            gpu_layers=model_data.get("gpu_layers"),
            estimated_vram_mb=model_data.get("estimated_vram_mb"),
            api_key=api_key,
            api_key_env=api_key_env,
        )

    resources_data = data.get("resources", {})
    resources = ResourceConfig(
        max_concurrent_gpu_agents=resources_data.get("max_concurrent_gpu_agents", 1),
        max_concurrent_cpu_agents=resources_data.get("max_concurrent_cpu_agents", 2),
    )

    verification_data = data.get("verification", {})
    verification = VerificationConfig(
        run_tests_after_changes=verification_data.get("run_tests_after_changes", True),
        max_fix_iterations=verification_data.get("max_fix_iterations", 3),
    )

    # Safe by default: an absent `approval:` section (or an absent key
    # within it) means risky actions still pause for a human decision.
    # Only an explicit `false` in the project's own config opts out.
    approval_data = data.get("approval", {})
    approval = ApprovalConfig(
        require_approval_for_commands=approval_data.get("require_approval_for_commands", True),
        require_approval_for_commits=approval_data.get("require_approval_for_commits", True)
    )

    # routing.roles and agentic.role_models are the same map; routing.roles
    # wins where both name a role.
    agentic_data = data.get("agentic", {})
    routing_data = data.get("routing", {}) or {}
    role_models = dict(agentic_data.get("role_models", {}) or {})
    role_models.update(routing_data.get("roles", {}) or {})
    agentic = AgenticConfig(
        role_models=role_models,
        context_window_chars=agentic_data.get("context_window_chars", 24000),
        compact_context_chars=agentic_data.get("compact_context_chars", 12000),
        max_parallel_agents=agentic_data.get("max_parallel_agents", 1),
        session_cache=agentic_data.get("session_cache", False),
    )

    fallbacks = {}
    for name, alternatives in (routing_data.get("fallbacks", {}) or {}).items():
        fallbacks[name] = [alternatives] if isinstance(alternatives, str) else list(alternatives or [])
    routing = RoutingConfig(escalate_to=routing_data.get("escalate_to"), fallbacks=fallbacks)

    tools_data = data.get("tools", {}) or {}
    tool_roles = {role: list(names or []) for role, names in (tools_data.get("roles", {}) or {}).items()}
    # The older `permissions: [{role, allowed_tools}]` list, documented in
    # config/config.yaml, means the same thing as tools.roles.
    for entry in data.get("permissions", []) or []:
        if isinstance(entry, dict) and entry.get("role"):
            tool_roles.setdefault(entry["role"], list(entry.get("allowed_tools") or []))
    tools = ToolSettings(
        disabled=list(tools_data.get("disabled", []) or []),
        command_timeout=tools_data.get("command_timeout"),
        roles=tool_roles,
    )

    return ProjectConfig(
        models=models,
        resources=resources,
        verification=verification,
        approval=approval,
        agentic=agentic,
        routing=routing,
        tools=tools,
        sources=sources,
        state_dir=data.get("state_dir", ".local-coder")
    )


def validate_config(config: ProjectConfig) -> list[str]:
    """Problems worth showing the user; none of them stop a run from starting."""
    from local_coder.types import AgentRole, ToolName

    problems = []
    known_models = set(config.models)
    known_roles = {role.value for role in AgentRole}
    known_tools = {tool.value for tool in ToolName}
    for role, model in config.agentic.role_models.items():
        if role not in known_roles and role != "drafter":
            problems.append(f"routing.roles: unknown role {role!r}")
        if model not in known_models:
            problems.append(f"routing.roles.{role}: model {model!r} is not defined under models")
    if config.routing.escalate_to and config.routing.escalate_to not in known_models:
        problems.append(f"routing.escalate_to: model {config.routing.escalate_to!r} is not defined under models")
    for name, alternatives in config.routing.fallbacks.items():
        for alt in [name, *alternatives]:
            if alt not in known_models:
                problems.append(f"routing.fallbacks.{name}: model {alt!r} is not defined under models")
    for name, model in config.models.items():
        if model.api_key_env and not model.api_key:
            problems.append(f"models.{name}: environment variable {model.api_key_env} is not set")
    for tool in config.tools.disabled:
        if tool not in known_tools:
            problems.append(f"tools.disabled: unknown tool {tool!r} (ignored)")
    for role, tools in config.tools.roles.items():
        if role not in known_roles:
            problems.append(f"tools.roles: unknown role {role!r}")
        for tool in tools:
            if tool not in known_tools:
                problems.append(f"tools.roles.{role}: unknown tool {tool!r} (ignored)")
    return problems

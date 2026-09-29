"""Pick which configured model serves a role or step.

Pure functions over ProjectConfig so the CLI's display (`local-coder config`,
`/agents`) and ModelManager.get_model agree on the answer.
"""
from __future__ import annotations

from local_coder.types import AgentRole, ProjectConfig


def resolve_model_name(
    config: ProjectConfig,
    role: AgentRole | str,
    model_name: str | None = None,
    *,
    escalate: bool = False,
) -> str:
    """Model name for a role.

    Priority: an explicit per-task model_name, then routing.escalate_to when
    the step is a retry after a failure, then the role map (routing.roles /
    agentic.role_models), then a model named after the role, then "default",
    then the first configured model.
    """
    role_name = role.value if isinstance(role, AgentRole) else role
    if model_name:
        selected = model_name
    elif escalate and config.routing.escalate_to:
        selected = config.routing.escalate_to
    else:
        selected = config.agentic.role_models.get(role_name)
    if selected is not None:
        if selected not in config.models:
            raise ValueError(f"Model not configured: {selected}")
        return selected
    if role_name in config.models:
        return role_name
    if "default" in config.models:
        return "default"
    if config.models:
        return next(iter(config.models))
    raise RuntimeError("No models configured.")


def fallback_chain(config: ProjectConfig, name: str) -> list[str]:
    """The model itself followed by its configured fallbacks, deduplicated."""
    chain = [name]
    for alt in config.routing.fallbacks.get(name, []):
        if alt in config.models and alt not in chain:
            chain.append(alt)
    return chain

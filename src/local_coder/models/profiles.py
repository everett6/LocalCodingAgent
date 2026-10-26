"""Bounded model/role scaffolds. Profiles reorder or remove existing capabilities."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


APPROVED_MIDDLEWARE = ("context_retrieval", "strategy_review", "verification")
APPROVED_STRATEGIES = ("inspect_then_edit", "hypothesis_then_test", "small_steps")
IMMUTABLE_GATES = ("permissions", "scope", "approval", "checkpoint", "budget")


@dataclass(frozen=True)
class ModelProfile:
    family: str
    role: str
    instructions: str
    preferred_tools: tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...] | None = None
    middleware: tuple[str, ...] = APPROVED_MIDDLEWARE
    strategy: str = "inspect_then_edit"
    max_strategy_attempts: int = 3

    def apply_tools(self, schemas: list[dict]) -> list[dict]:
        """Intersect with the engine's permitted schemas; never manufacture a schema."""
        priorities = {name: index for index, name in enumerate(self.preferred_tools)}
        def name(schema):
            return schema.get("function", {}).get("name", "")
        narrowed = [s for s in schemas if self.tool_allowlist is None or name(s) in self.tool_allowlist]
        return sorted(narrowed, key=lambda s: priorities.get(name(s), len(priorities)))


def validate_overlay(overrides: dict[str, Any]) -> dict[str, Any]:
    """Only declarative, known controls may cross the profile boundary."""
    allowed = {"instructions", "preferred_tools", "tool_allowlist", "middleware", "strategy", "max_strategy_attempts"}
    if not isinstance(overrides, dict) or set(overrides) - allowed:
        raise ValueError("Unknown profile controls; immutable engine gates cannot be changed")
    result = dict(overrides)
    for key in ("preferred_tools", "tool_allowlist", "middleware"):
        if key not in result:
            continue
        value = result[key]
        if not isinstance(value, (list, tuple)) or len(value) > 128 or any(not isinstance(v, str) or len(v) > 100 for v in value):
            raise ValueError(f"{key} must be a bounded list of names")
        if key == "middleware" and (len(set(value)) != len(value) or set(value) - set(APPROVED_MIDDLEWARE)):
            raise ValueError("Only approved middleware may be selected, once each")
        result[key] = tuple(value)
    if "instructions" in result and (not isinstance(result["instructions"], str) or len(result["instructions"]) > 16000):
        raise ValueError("instructions must be bounded text")
    if "strategy" in result and result["strategy"] not in APPROVED_STRATEGIES:
        raise ValueError("Unknown strategy")
    if "max_strategy_attempts" in result:
        value = result["max_strategy_attempts"]
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError("max_strategy_attempts must be an integer from 1 to 10")
    return result


def resolve_profile(model_config: Any, role: Any, overrides: dict | None = None) -> ModelProfile:
    """Resolve just the requested family/role; explicit user controls remain bounded."""
    role_name = getattr(role, "value", str(role))
    model_id = getattr(model_config, "model_id", "").lower()
    configured = getattr(model_config, "profile", "generic")
    hints = configured if configured != "generic" else model_id
    family = next((name for name in ("deepseek", "glm", "kimi", "qwen") if name in hints), "generic")
    instructions = {
        "generic": "Inspect the relevant context, make a small change, and verify its observable result.",
        "deepseek": "State a concise working hypothesis. Use targeted repository evidence before choosing a tool; summarize test evidence after each strategy.",
        "glm": "Keep tool arguments explicit and complete. Read exact surrounding text before edits, then inspect the resulting diff and focused checks.",
        "kimi": "Split long context into bounded evidence groups. Track file references explicitly, finish one scoped objective, and verify before expanding scope.",
        "qwen": "Prefer short inspect/edit/check cycles. Ground edits in exact source spans and use the smallest relevant verification tool.",
    }[family]
    role_instruction = {
        "explorer": "Return evidence and source locations for the parent task.",
        "planner": "Propose ordered changes and measurable acceptance checks from repository evidence.",
        "coder": "Implement the requested behavior with focused edits and relevant verification.",
        "debugger": "Form one falsifiable hypothesis and reproduce it before changing the implementation.",
        "tester": "Exercise externally observable behavior and report failures with reproduction evidence.",
        "reviewer": "Report actionable defects with concrete reachability and file references.",
        "security": "Review only the authorized local repository; record exploit paths and evidence, respecting suppressed findings.",
        "exploit_validator": "Reproduce an existing finding only within the inherited local test scope.",
        "red_team": "Honor the inherited engagement and local-only scope on every action.",
        "bug_hunter": "Search boundary cases, reproduce failures, and verify minimal fixes.",
        "orchestrator": "Delegate bounded objectives within inherited scope and shared budgets; verify returned evidence.",
    }.get(role_name, "Remain within the caller's existing permissions and scope.")
    strategy = "hypothesis_then_test" if role_name in {"debugger", "bug_hunter", "security"} else "inspect_then_edit"
    preferred = {
        "deepseek": ("code_search", "read_file", "apply_patch", "run_tests"),
        "kimi": ("code_search", "language_context", "memory_search", "read_file", "edit_file", "run_tests"),
    }.get(family, ("code_search", "read_file", "edit_file", "run_tests"))
    if role_name in {"security", "red_team", "bug_hunter"}:
        preferred = ("security_playbook", "redteam_workflow", "run_sast", "run_bug_scan") + preferred
    profile = ModelProfile(family, role_name, instructions + "\n" + role_instruction,
                           preferred, strategy=strategy,
                           max_strategy_attempts=min(10, max(1, getattr(model_config, "strategy_attempt_limit", 3))))
    if overrides is not None:
        overlay = validate_overlay(overrides)
        # A profile can reduce the model's configured strategy budget, never increase it.
        if "max_strategy_attempts" in overlay:
            overlay["max_strategy_attempts"] = min(profile.max_strategy_attempts, overlay["max_strategy_attempts"])
        profile = replace(profile, **overlay)
    return profile

"""Bounded model/role scaffolds. Profiles reorder or remove existing capabilities."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Any

from local_coder.models.specialization import FAMILY_POLICIES, WORKFLOWS, capability_facts, find_model, model_index


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
    identity: str = "generic-unverified"
    workflow: tuple[str, ...] = ()
    context_share_percent: int = 10
    catalog_char_limit: int = 6000
    tool_result_char_limit: int = 8000
    provenance: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)

    def describe(self) -> dict:
        """Bounded public diagnostics; never serialize model configuration/keys."""
        return {"identity": self.identity, "family": self.family, "role": self.role,
                "workflow": list(self.workflow), "strategy": self.strategy,
                "context_share_percent": self.context_share_percent,
                "catalog_char_limit": self.catalog_char_limit,
                "tool_result_char_limit": self.tool_result_char_limit,
                "max_strategy_attempts": self.max_strategy_attempts,
                "provenance": dict(self.provenance), "capabilities": dict(self.capabilities)}

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
    model_id = str(getattr(model_config, "model_id", ""))
    configured = getattr(model_config, "profile", "generic")
    row = find_model(model_id)
    hints = str(configured if configured != "generic" else model_id).casefold()
    # Heuristics may select conservative family defaults but never claim index membership.
    inferred = next((name for name in FAMILY_POLICIES if name != "generic"
                     and re.search(r"(?:^|[/_\W])" + name + r"(?:\d|[/_\W]|$)", hints)), "generic")
    family = row["family"] if row and configured == "generic" else inferred
    instructions, catalog_cap, memory_share, result_cap = FAMILY_POLICIES.get(family, FAMILY_POLICIES["generic"])
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
    if role_name in {"coder", "debugger"}:
        preferred = tuple(dict.fromkeys(("read_file", "edit_file", "write_file", "run_command", "code_search") + preferred))
    workflow = ("Inspect relevant sources", "Make a focused change", "Verify observable behavior")
    if role_name in WORKFLOWS:
        workflow, role_tools, role_instruction = WORKFLOWS[role_name]
        preferred = tuple(dict.fromkeys(role_tools + preferred))
        # Red-team evidence is refreshed through its bound engagement, not shared memory.
        if role_name == "red_team":
            strategy, memory_share = "small_steps", 5
        elif role_name == "security":
            memory_share = min(20, memory_share + 2)
    facts = capability_facts(row, model_config)
    window = getattr(model_config, "context_length", 8192)
    window = window if type(window) is int and window > 0 else 8192
    if facts.get("context_window"):
        window = min(window, facts["context_window"])
    # Physical model capacity can only narrow policy; user/context-ledger clamps follow.
    if window <= 32768:
        catalog_cap, memory_share, result_cap = min(catalog_cap, 6000), min(memory_share, 10), min(result_cap, 6000)
    elif window <= 131072:
        catalog_cap, result_cap = min(catalog_cap, 10000), min(result_cap, 10000)
    provenance = {"match": "exact_snapshot_identity" if row else "family_heuristic" if family != "generic" else "generic_fallback",
                  "policy_basis": "curated_defaults_not_quality_benchmarks"}
    if row:
        snapshot = model_index()
        provenance.update(source_url=snapshot["source_url"], retrieved_at=snapshot["retrieved_at"],
                          model_rank=row["model_rank"], leaderboard_row=row["rank"],
                          variant_count=len(row["variants"]), provider_mapping=row["identity_mapping_status"])
    if configured != "generic":
        provenance["family_override"] = configured
    profile = ModelProfile(family, role_name, role_instruction + "\n" + instructions,
                           preferred, strategy=strategy,
                           max_strategy_attempts=min(10, max(1, getattr(model_config, "strategy_attempt_limit", 3))),
                           identity=row["name"] if row else family + "-unverified", workflow=workflow,
                           context_share_percent=memory_share, catalog_char_limit=catalog_cap,
                           tool_result_char_limit=result_cap, provenance=provenance, capabilities=facts)
    if overrides is not None:
        overlay = validate_overlay(overrides)
        # A profile can reduce the model's configured strategy budget, never increase it.
        if "max_strategy_attempts" in overlay:
            overlay["max_strategy_attempts"] = min(profile.max_strategy_attempts, overlay["max_strategy_attempts"])
        profile = replace(profile, **overlay)
    if facts.get("tools") is False:
        profile = replace(profile, tool_allowlist=(), instructions=profile.instructions +
                          "\nThis endpoint's catalog does not advertise tools; report the capability blocker without inventing tool results.")
    return profile

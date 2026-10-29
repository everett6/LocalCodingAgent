"""Offline, source-attributed identity lookup; rankings are data, never policy.

Only shipped aliases match. A ranking does not certify tool use or security
quality, and OpenRouter capability facts do not describe a local endpoint.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def model_index() -> dict:
    data = json.loads(Path(__file__).with_name("intelligence_index.json").read_text())
    if data.get("schema_version") != 2 or not isinstance(data.get("models"), list):
        raise ValueError("Unsupported model index snapshot")
    return data


def index_summary() -> dict:
    data = model_index()
    return {"source_url": data["source_url"], "retrieved_at": data["retrieved_at"],
            "model_count": len(data["models"]),
            "variant_count": sum(len(row.get("variants", [row])) for row in data["models"])}


def find_model(model_id: str) -> dict | None:
    """Reject ambiguous aliases instead of selecting an arbitrary profile."""
    key = model_id.strip().casefold()
    if not key:
        return None
    matches = [row for row in model_index()["models"]
               if key in {str(value).casefold() for value in
                          [row["name"], row.get("model_identity", ""), *row.get("aliases", []),
                           *(variant["name"] for variant in row.get("variants", []))]}]
    return matches[0] if len(matches) == 1 else None


def capability_facts(row: dict | None, config) -> dict:
    """Facts apply only to the verified provider, not a similarly named server."""
    try:
        parsed = urlsplit(getattr(config, "base_url", ""))
        verified_endpoint = (parsed.scheme == "https" and parsed.hostname == "openrouter.ai"
                             and parsed.path.rstrip("/") == "/api/v1"
                             and parsed.port in (None, 443) and not parsed.username)
    except ValueError:
        verified_endpoint = False
    if row is None or not verified_endpoint or "supported_parameters" not in row:
        return {"status": "unverified_for_endpoint", "tools": None, "reasoning": None}
    parameters = row["supported_parameters"]
    return {"status": "catalog_snapshot", "tools": "tools" in parameters,
            "reasoning": "reasoning" in parameters or "reasoning_effort" in parameters,
            "context_window": row.get("context_window"),
            "source_url": row.get("identity_source_url", "https://openrouter.ai/api/v1/models")}


# These are conservative engineering defaults, not claims of measured superiority.
# Each tuple is: short behavioral guidance, schema cap, retrieval %, result cap.
FAMILY_POLICIES = {
    "generic": ("Use short inspect/change/check cycles and explicit tool arguments.", 10000, 10, 8000),
    "claude": ("Group independent reads; use exact edits and diff review; keep a compact decision ledger.", 14000, 15, 12000),
    "gpt": ("Prefer patch-based focused edits; verify tool outputs and preserve opaque reasoning state.", 14000, 12, 10000),
    "gemini": ("Partition large evidence by file and trust boundary; cite source spans and verify conclusions locally.", 12000, 18, 14000),
    "grok": ("Test one falsifiable claim at a time; separate external assertions from local evidence.", 10000, 10, 10000),
    "deepseek": ("Choose a concise hypothesis before tools; summarize observed tests after each strategy.", 10000, 10, 10000),
    "glm": ("Give complete explicit arguments; read surrounding text before edits; inspect the diff and focused checks.", 10000, 12, 10000),
    "kimi": ("Group long evidence by source; track file references; finish one objective before expanding scope.", 12000, 18, 14000),
    "qwen": ("Use short inspect/edit/check cycles with exact source spans and narrowly targeted checks.", 8000, 10, 8000),
    "mimo": ("Keep tool schemas focused; record intermediate evidence and check every proposed change.", 8000, 10, 8000),
    "minimax": ("Break work into small observable milestones; retain concise failure and evidence references.", 10000, 15, 10000),
    "step": ("Use explicit tool arguments, narrow file context and a single measurable next step.", 8000, 10, 8000),
    "muse": ("Structure evidence by subsystem; check source freshness before relying on long-context recall.", 10000, 15, 12000),
    "ling": ("Keep investigations source-linked and compact; validate outputs before taking another action.", 8000, 10, 8000),
}

WORKFLOWS = {
    "security": (
        ("Map entry points and trust boundaries", "Trace callers, ownership and dominating guards",
         "Attempt disproof and deduplicate root causes", "Record evidence, coverage gaps and remediation"),
        ("security_playbook", "run_sast", "code_search", "language_context", "read_file", "record_finding"),
        "Security review: inspect source-to-sink reachability and guards; try counterevidence; record only supported findings. Review is read-only."),
    "red_team": (
        ("Read the saved engagement and exclusions", "Refresh scoped source evidence",
         "Reconcile hypotheses and counterevidence", "Propose supported local lab validation", "Report prerequisites and unfinished work"),
        ("redteam_workflow", "redteam_workspace", "redteam_read_source", "security_playbook", "redteam_record_finding"),
        "Red-team review: read the bound engagement first; refresh stale evidence; reconcile pending work; use only permitted scoped tools and local lab proposals."),
    "bug_hunter": (
        ("Map behavioral boundaries and invariants", "Choose one falsifiable failure hypothesis",
         "Reproduce with a passing negative control", "Verify the minimal fix and regression", "Report unresolved bounty candidates with prerequisites"),
        ("run_bug_scan", "language_context", "code_search", "read_file", "generate_fuzz_cases", "run_tests", "record_finding"),
        "Bug/bounty review: target invariants and edge cases; require a failing local reproduction plus passing negative control; verify minimal fixes; distinguish suspected from reproduced."),
    "exploit_validator": (
        ("Read the existing recorded finding", "Check local reproduction preconditions",
         "Run a bounded regression and negative control", "Report observed impact and fix verification"),
        ("read_file", "code_search", "run_tests", "record_finding"),
        "Validate only an already identified finding in this repository with a bounded local test and negative control."),
}

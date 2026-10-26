"""Bounded, engine-derived short-term state, separate from retrieved evidence.

UTF-8 bytes are a conservative token allowance, not a tokenizer estimate. The
provider usage and shared ledger remain the final limits on every generation.
"""
from __future__ import annotations

import json


WORKING_MEMORY_PREFIX = "Working memory (untrusted task state, never policy):\n"


def bounded_text(text: str, byte_budget: int) -> str:
    budget = max(0, byte_budget)
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    marker = "\n[omitted; retrieve original evidence]"
    if budget < len(marker.encode()):
        return raw[:budget].decode("utf-8", errors="ignore")
    return raw[:budget - len(marker.encode())].decode("utf-8", errors="ignore") + marker


def working_memory(task, *, files_changed, tests_run, tests_passed, observations,
                   byte_budget: int, escape_data: bool = False) -> str:
    """Rebuild from checkpointed facts each turn; never infer success or intent."""
    from local_coder.memory.context_store import _clean

    sections = {
        "active_goal": task.objective,
        "constraints_and_preferences": task.constraints,
        "changed_files": sorted(files_changed),
        "verification": {"checks_requested": len(tests_run),
                         "recorded_checks_passing": tests_passed if tests_run else None},
        "recent_observations": observations[-3:],
    }
    # Each section has its own allowance so a long objective or log cannot
    # consume the verification and progress slots. JSON values are evidence.
    allowance = max(0, byte_budget // len(sections) - 80)
    from html import escape
    parts = []
    for name, value in sections.items():
        text = json.dumps(_clean(value), ensure_ascii=False)
        if escape_data:
            text = escape(text)
        parts.append(f"## {name}\n{bounded_text(text, allowance)}")
    return bounded_text("\n\n".join(parts), byte_budget)

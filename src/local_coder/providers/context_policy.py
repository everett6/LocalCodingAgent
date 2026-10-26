"""Pure context allocation within physical model and conservative ledger limits.

These allowances are capacity planning, not a tokenizer or an admission bypass.
The request ledger still measures each serialized request before generation.
"""

from __future__ import annotations

from local_coder.types import AgentMode

# Output share, prompt utilization, memory share, and compacted-history share.
# Security retains more evidence; coding leaves more room for generated code.
_MODES = {
    AgentMode.CODING: (30, 90, 8, 55),
    AgentMode.CYBERSECURITY: (20, 98, 12, 65),
    AgentMode.RED_TEAM: (25, 95, 10, 60),
    AgentMode.BUG_HUNTING: (15, 92, 10, 60),
}


def _get(value, field, default):
    return (
        value.get(field, default)
        if isinstance(value, dict)
        else getattr(value, field, default)
    )


def _integer(value, name, low=1, high=1_000_000_000):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be a bounded integer")
    return value


def context_policy(config, mode="coding", harness=None):
    """Return allocations without reading files, connecting, or mutating inputs.

    ``prompt_tokens`` is a conservative UTF-8/JSON byte allowance for message
    content. Six bytes per character also covers JSON-escaped control characters;
    it is intentionally more conservative than a model-specific token estimate.
    The separate overhead reserves system instructions, tool schemas, message
    envelopes, and the ledger's fixed accounting allowance. Large catalogs or
    irreducible prompts can still be refused by the runtime admission checks.

    Hosts apply these values only when ``auto_context`` is enabled. Manual
    context settings remain the caller's responsibility and are never changed.
    """
    try:
        mode = AgentMode(mode)
    except (ValueError, TypeError):
        raise ValueError("Unknown agent mode for context allocation") from None
    model_limit = _integer(_get(config, "context_length", 8192), "Model context", low=8)
    output_limit = _integer(_get(config, "max_tokens", 4096), "Model output")
    request_limit = _integer(
        _get(harness, "max_request_tokens", 131072), "Per-request limit", low=8
    )
    task_limit = _integer(
        _get(harness, "max_tokens", 1_000_000), "Task token limit", low=8
    )
    effective_limit = min(model_limit, request_limit, task_limit)
    overhead = min(8192, effective_limit // 4)
    usable = effective_limit - overhead
    output_share, utilization, memory_share, compact_share = _MODES[mode]
    # Keep enough space for at least one worst-case escaped input character.
    completion = max(
        1, min(output_limit, max(1, usable * output_share // 100), usable - 6)
    )
    prompt = max(6, (usable - completion) * utilization // 100)
    # Eight-token toy limits cannot fit a 6-byte character plus all reserves;
    # shrink overhead rather than silently exceed the physical request limit.
    overhead = min(overhead, max(0, effective_limit - prompt - completion))
    if prompt + completion + overhead > effective_limit:
        raise ValueError("Context is too small to allocate prompt and completion")
    window = max(1, prompt // 6)
    compact = max(1, window * compact_share // 100)
    if window > 1:
        compact = min(compact, window - 1)
    memory_enabled = _get(harness, "memory_enabled", True)
    if type(memory_enabled) is not bool:
        raise ValueError("memory_enabled must be a boolean")
    memory_limit = _integer(
        _get(harness, "memory_context_chars", 3000),
        "Memory allowance",
        low=0,
        high=12000,
    )
    memory = (
        min(memory_limit, max(1, window * memory_share // 100))
        if memory_enabled and memory_limit
        else 0
    )
    source = _get(config, "context_source", "configured")
    if (
        not isinstance(source, str)
        or not 1 <= len(source) <= 100
        or any(ord(character) < 32 for character in source)
    ):
        raise ValueError("Context source must be bounded descriptive text")
    return {
        "model_limit": model_limit,
        "request_limit": effective_limit,
        "mode": mode.value,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reserved_overhead_tokens": overhead,
        "memory_chars": memory,
        "source": source,
        "context_window_chars": window,
        "compact_context_chars": compact,
    }

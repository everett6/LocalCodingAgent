# Provider completion and accounting audit

Verified 2026-10-29 UTC with synthetic HTTP transports. No remote model call, paid request or external target was used. The audit covers the OpenAI-compatible/Anthropic adapters and their shared budget/telemetry boundary; it does not claim a complete provider or billing audit.

## Confirmed defects and fixes

1. **Truncated tool calls remained executable.** Both adapters returned syntactically valid tool calls even when the completion's terminal reason indicated a token limit. Because the agent's old truncation check lived in its no-tool branch, partial arguments could reach the permitted tool pipeline. The adapters now suppress these calls while retaining numeric usage and the original truncation reason. Filtered OpenAI completions fail closed. This follows the distinction between normal, tool and truncated completion reasons in the [OpenAI reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events) and the interrupted-tool handling documented by the [official Node SDK](https://github.com/openai/openai-node/blob/main/docs/helpers.md). Host scope and approval gates remain necessary; these changes do not replace them.

2. **Partial text streams appeared successful.** EOF without a recognized terminal event previously ended both text iterators normally. The adapters now report incomplete streams, reject malformed JSON events without echoing the payload, and preserve failed budget reservations. Anthropic requires `message_stop`; the protocol's event sequence is described in the [official streaming documentation](https://platform.claude.com/docs/en/build-with-claude/streaming). OpenAI accepts an explicit finish reason or the compatibility `[DONE]` marker. The optional space after `data:` is supported. Existing partial text may already have been displayed; it is not retroactively labeled verified.

3. **Unexpected stream choices were combined.** OpenAI-compatible requests use the single-choice default, but stream collection aggregated every returned branch. A synthetic choice at index 1 supplied a complete tool call before the fix. Generation and text streaming now reject unexpected choice indexes or multiple choices rather than merge them. Usage-only empty-choice frames and omitted index-0 fields remain supported for compatible servers.

4. **Partial/coerced usage released reservations.** Missing counts became zero, and Pydantic could turn numeric strings or booleans into integers before settlement. Adapters now accept reported usage only when complete nonnegative integer counts are present. OpenAI totals must agree when supplied. Unknown or malformed usage keeps the admitted reservation. Explicit complete zero usage has a separate validated marker, so fixed request fees survive settlement.

5. **Anthropic cached inputs were omitted.** Prompt counts now include uncached, cache-created and cache-read tokens. Model responses expose only a bounded numeric category map, not arbitrary provider payloads. The [official caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) distinguishes these categories and their model/platform/lifetime-dependent prices. The harness does not invent universal cache rates: when cached costs cannot be derived from the configured ordinary rates, it retains the admitted dollar amount as an **estimate**, sets `cost_known: false`, omits a precise telemetry cost, and refuses subsequent requests under a dollar cap. Configured all-zero pricing remains known free. The first request's bill cannot be guaranteed by an unconfigured cache-price schedule; this is an explicit limit, not a hard billing guarantee.

6. **Settlement reread mutable prices.** A backend could observe different rates from those admitted, including invalid rates. Reservations now retain a numeric price snapshot, and settlement/telemetry use the pre-generation prices. Overflowing admission estimates are rejected, and nonfinite settled costs cannot enter a valid JSON cost total.

7. **Independent review found numeric overflow paths.** A synthetic 401-digit provider token count could overflow settlement after changing in-memory status, while disk retained the old reservation. OpenAI-compatible and Anthropic usage totals now have an explicit trillion-token upper bound, and the host ledger/telemetry wrapper independently enforces the same boundary for custom backends. Invalid counts retain the reservation. Huge integer prices are rejected before admission. Telemetry also handles conversion/product overflow without storing Infinity or losing earlier rows. Five additional numeric regressions cover provider and custom adapters, disk-backed accounting and prior diagnostic retention.

The host agent now rejects length/max-token responses before tool repair for every backend, closes their unexecuted protocol records and saves an explicit truncated-output checkpoint. Adapter checks are supplemental to this host boundary.

The two provider integrity/accounting findings are recorded in the private project ledger as F4/F5. Human-reviewed security lessons were not modified.

## Verification

**268 combined routing/provider/quota/context tests passed**, including **27 new provider-integrity regressions**. Existing Anthropic stream fixtures now include the documented terminal event. No automatic provider retry, mutating-tool replay, model download or provider switch was added.

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_provider_completion_integrity.py tests/test_openai_compat.py \
  tests/test_cloud_providers.py tests/test_reasoning_harness.py \
  tests/test_quota_integration.py tests/test_openrouter_profiles.py \
  tests/test_token_accounting.py tests/test_context_50.py \
  tests/test_auto_routing_api.py tests/test_auto_routing_runtime.py \
  tests/test_auto_routing.py tests/test_auto_routing_coordinator.py tests/test_routing.py
```

Remaining limitations include unbounded provider response bodies, general multi-line SSE event support, provider-specific cache billing, and positive availability results remaining cached for the model manager. The text-only stream interface does not expose final provider usage; it conservatively retains reservations. Final integrated test counts, including independent-review fixes, are recorded in the block's delivery report.

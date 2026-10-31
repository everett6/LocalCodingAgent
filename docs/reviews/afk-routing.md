# Routing reliability and allocation audit

Verified 2026-10-29 UTC using local fixtures and synthetic providers. No real model inference, paid request, provider probe or runtime installation was used.

## Changes

- Economy eligibility diagnostics now use the same pure endpoint and tool-capability gate as the runtime. A configured model is no longer reported as available when local-to-cloud routing, an unapproved provider boundary, an invalid endpoint or confirmed missing tool support would prevent its execution. Eligibility does not assert provider reachability or model quality.
- Local classifier status uses the same loopback/OpenAI-compatible or llama.cpp admission as execution. An Ollama or Anthropic configuration, a missing model key, or a non-loopback endpoint cannot be shown as ready. Explicitly unsupported configurations have `classifier_state: "unsupported"`. A loaded model must also match the model identity and effective endpoint. Equivalent explicit/default ports and trailing slashes normalize consistently.
- Routing preference updates now flush a private temporary file and atomically replace the prior snapshot. Flush or replacement failure preserves the prior valid preferences and removes the temporary file. The existing idle-engine lock still rejects overlapping changes; unrelated saved preferences survive partial updates. This adds neither a cross-process preference lock nor a universal power-loss guarantee.
- Routing no longer joins and UTF-8 encodes an entire large task context merely to apply a 2,400-character policy allowance. Every input byte still contributes to the same complete digest, but the retained policy buffer is bounded and large inputs fail closed before regex scanning. Large-task reason metadata is now `ambiguous_or_long`, even if a later source contains a security keyword. Protected roles/modes and selected-model behavior remain unchanged.
- Failed model availability probes now expire after five seconds, so a configured server that starts or recovers during a run can be reconsidered. Concurrent checks of the same model share one probe, repeated checks during an outage reuse the negative result, and cancellation releases the probe lock. Positive results retain the existing per-manager cache behavior. Closing a manager clears its routing and reachability caches before a later backend initialization.

## Evidence

The initial API regressions demonstrated false availability/readiness before the change. The combined targeted suite now passes **144 tests**:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_auto_routing_api.py tests/test_auto_routing_runtime.py \
  tests/test_auto_routing.py tests/test_auto_routing_coordinator.py tests/test_routing.py
```

Coverage includes endpoint/privacy boundaries, unsupported classifier backends, synthetic budget/quota accounting, bounded classifier replies and cancellation, cache isolation, protected/retry workflows, fallback price constraints, atomic-save failure recovery, concurrent-save rejection, source-tail digest changes and Unicode fidelity. Simulated-clock tests verify recovered-server selection, outage probe frequency, concurrent probe coalescing, cancelled probe recovery and cache clearing, without real network calls or five-second sleeps.

A single-process `tracemalloc` comparison against the previous committed policy used a preallocated 2 MiB synthetic architecture observation. Additional peak allocations fell from **4,198,485 bytes** to **20,836 bytes**. Both paths returned the selected tier and identical SHA-256 digest. One illustrative timing was 0.313540 seconds versus 0.001233 seconds; this is a microbenchmark on this host, not an end-to-end latency or token-saving guarantee. Hashing still reads all source characters, and the existing task context remains resident.

Actual classifier confidence calibration, on-device model accuracy, provider availability, total inference cost and permission-model calibration remain unmeasured. These changes do not grant tools, weaken scopes or turn classifier output into approval.

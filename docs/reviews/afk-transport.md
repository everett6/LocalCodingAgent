# Bounded provider transport and SSE framing

Verified 2026-10-29 UTC. This phase changes the OpenAI-compatible and Anthropic adapters and a shared provider-response helper. It adds no retries, provider switches, inference calls or automatic tool replay. All HTTP tests use synthetic transports; no external target or paid model was contacted.

## Implemented behavior

Generation now opens an HTTPX streaming context before reading a response. The helper counts raw body bytes before retaining them, rather than checking size after `.post()` has buffered the entire body. The relevant distinctions between raw iteration, decoded iteration and automatic context closure are documented in [HTTPX's async streaming reference](https://www.python-httpx.org/async/).

| Body | Limit | Behavior |
| --- | --- | --- |
| Nonstreaming completion JSON | 16 MiB | Reject before accumulating beyond the cap; decode and parse only the bounded body |
| Optional model metadata | 1 MiB | Reject oversized/unsupported metadata and retain basic configured model information |
| SSE stream body | 64 MiB | Count every body chunk, including comments and ignored fields |
| SSE event | 1 MiB | Bound line/event buffering, including unknown fields and comments, before dispatch |
| HTTP 429 quota-error body | 128,000 bytes | Read only within the existing quota observer's allowance; oversized/unsupported bodies fail closed |
| Other HTTP errors, availability lists and slot-operation responses | No body read | Use the status and close the response |

These are generous application defaults rather than model-token conversions: JSON must allow reasoning, tool arguments and metadata, while SSE reserves extra space for repeated envelopes and keepalive fields. A legitimate completion or individual tool argument exceeding a cap will fail explicitly. The stream limit includes protocol overhead, so it is larger than the nonstreaming completion allowance. These are serialized-body limits, not an OS memory quota or a bound on parsed Python object overhead.

Requests explicitly ask for `Accept-Encoding: identity`. Responses declaring gzip, deflate, another encoding or an encoding chain are rejected **before raw body iteration**. The helper does not invoke HTTPX's decompression iterator, avoiding a compressed-body expansion before the byte check. This intentionally sacrifices compatibility with servers that ignore the identity request. Body counts exclude TLS, HTTP header and transfer-framing overhead. A transport can deliver one chunk crossing a cap; that chunk is rejected before accumulation, and no later chunks are requested. A custom transport that already buffered/decompressed a response cannot have that prior work undone by this helper.

The SSE parser follows the [WHATWG framing rules](https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation) for multiline data, one optional field-value space, blank-line dispatch, comments, an initial BOM and CR/LF/CRLF endings across chunk boundaries. Unterminated events are discarded at EOF; provider-specific terminal checks remain authoritative. The parser intentionally rejects invalid UTF-8 instead of replacing bytes inside possible tool arguments. Event IDs, event names and reconnection hints do not trigger persistence or another request. Large transport chunks are subdivided before parser normalization to bound temporary parser work.

Tool-call fragments now accumulate in lists and join once, avoiding repeated concatenation of a growing argument string. Existing reasoning content/details, usage frames, tool-call integrity checks and daily-quota observation remain in place. Errors do not echo raw provider bodies. Cancellation and early exits close the HTTP response; attempted requests retain their conservative budget reservation.

## Evidence

**157 focused provider/quota/context tests passed**, including **33 new transport regressions**. Tests use `httpx.AsyncByteStream` implementations that count consumed chunks and closure, rather than relying only on prebuffered mock bodies. They cover default-cap early termination, declared oversize, gzip/deflate bombs rejected with zero body reads, multiline events, one-byte UTF-8/CRLF splits, large events and aggregate stream limits, bounded error observation, cancellation, unused bodies, optional metadata fallback, EOF tool-call suppression, complete reasoning/tool/usage reconstruction, and many small argument fragments.

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_provider_transport.py tests/test_provider_completion_integrity.py \
  tests/test_openai_compat.py tests/test_cloud_providers.py tests/test_reasoning_harness.py \
  tests/test_quota_integration.py tests/test_openrouter_profiles.py \
  tests/test_token_accounting.py tests/test_context_50.py
```

Compilation and whitespace checks pass. The confirmed unbounded-body issue is recorded in the private security ledger. No live-provider throughput, compressed-provider interoperability or host-wide memory guarantee was verified. Ollama and other unrelated HTTP clients are outside this transport phase.

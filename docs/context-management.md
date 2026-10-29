# Context management

The prompt has separate system policy, task context, working memory, retrieved
project evidence, and recent conversation messages. Retrieved text and the
working-memory block use the user role and carry explicit evidence labels;
they cannot create engine permissions or alter approval state.

Each turn regenerates working memory from checkpointed state: active objective,
user constraints, changed files, requested checks and recent observations. It
replaces the previous block instead of appending another copy. No requested checks
means an unknown verification result, rather than an assumed pass. Existing run
checkpoints persist these source fields across resume; no second scratchpad file
needs synchronization.

Working memory receives at most 10% of the message allowance and 2400 UTF-8
bytes, divided between its sections. Retrieved evidence receives at most 20% of
the message allowance and the configured memory limit. Existing serialized-message
limits reserve the tool catalog and output allowance, while the shared request
ledger reserves conservative UTF-8 bounds for every model call. These bounds are
not tokenizer counts. Oversized immutable policy or objectives still cause a
budget error rather than silently losing user constraints.

Recent complete assistant/tool exchanges stay intact during compaction. Older
repeated reads and tool logs are pruned first; model-written rolling summaries
then retain goals, constraints, decisions, source references, verification,
unresolved errors and next actions. Exact earlier evidence remains recoverable
through context archives. Failed summaries use the existing bounded fallback.

Long-term project evidence remains in `ContextMemory`, with redaction,
provenance, relevance filtering and freshness checks. Default retrieval uses
lexical hashes; an application can inject a local embedding backend for neural
semantic retrieval. No vector model is downloaded implicitly. The existing role
and objective ranked tool catalog narrows schemas when the model window requires
it; profiles can further restrict the allowed tools.

See [project memory](context-memory.md) and [adaptive harness](adaptive-harness.md)
for persistence, retrieval and approved revision controls.

## Data boundaries and strict allocation

Retrieved evidence and working memory are enclosed in `<retrieved_context>` and
`<working_memory>` tags. Their contents are escaped so embedded closing tags
cannot end the engine's wrapper. These labels aid model interpretation; role
placement, schema validation and engine permissions enforce the actual boundary.
A short fixed engine reminder is replaced every five iterations, rather than
accumulating copies or repeating untrusted text. The first request after resume
retains the checkpoint's working-memory block so its protocol state is unchanged;
subsequent turns regenerate the block.

Applications can enable stricter allocations in project YAML:

```yaml
harness:
  context_allocation:
    system: 15
    evidence: 45
    dialogue: 40
  context_snapshots: true
```

Percentages must sum to 100. Limits apply to serialized UTF-8 message sizes after
tool/output space is reserved. System messages use the system segment; tool
results and retrieved project memory use evidence; remaining messages, including
the original task and its supplied context, use dialogue. Exceeding a segment
stops generation with an explicit error instead of cutting policy or silently
borrowing another segment's budget. This policy is opt-in to preserve existing
projects with large immutable task preambles. The global, scratchpad and retrieval
limits apply without it.

The tool registry validates supplied arguments against each tool's JSON Schema
before scope checks, hooks, snapshots or execution. Unknown top-level fields,
incorrect types, missing required values and caller-supplied engine fields fail
closed. Repairable model calls may be normalized by the existing agent adapter,
but the normalized call must still pass the registry gateway. Permission, path,
engagement and approval checks remain separate mandatory gates.

## Local debugging

`context_snapshots` is off by default. When enabled, the runtime stores complete
redacted message payloads in private, content-addressed context archives before
generation and emits a `context_snapshot` event containing a reference, iteration,
serialized message/schema sizes and the prior provider-reported prompt count.
Normal model response events and metrics record actual usage when available.
Snapshots inherit the archive storage limit and must be cleaned or exported when
that limit is reached. Redaction is best effort; review exports before sharing.

The existing run state and complete-round checkpoints serialize message/tool
protocol state, budgets, counters, changed files and observations for recovery.
Diagnostic snapshots are not arbitrary mid-tool restart points: an incomplete
tool batch must be resolved by the existing recovery protocol. Attention weights
are not exposed by standard model APIs, so no attention distributions are logged
or inferred from token counts.

For machines with a read-only home directory, set
`LOCAL_CODER_CONNECTION_DIR` to an absolute writable directory for authenticated
backend discovery. Set the same variable for the desktop client. Connection
records retain owner-only permissions and are removed when the server closes.

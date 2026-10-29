# Plan-local actor messaging

The local mailbox adds an asynchronous communication primitive for independent
tasks in one host-created plan. It extends the existing DAG scheduler; it does
not replace scheduling, create worker processes, or grant new execution rights.

`PlanMailbox` takes a fixed list of up to 64 unique task IDs. Its default limits
are 16 queued messages per recipient, 256 sends over the plan's lifetime, 2,000
characters per message, and 128,000 serialized UTF-8 bytes over that lifetime.
Draining messages does not reset lifetime quotas. A full queue or exhausted quota
rejects the send explicitly rather than dropping an earlier message. Constructor
limits also have hard upper bounds. Serialized bytes are an accounting budget,
not a claim that the Python process's heap allocation equals that number.

Channels use deques and `asyncio.Event` notifications on the owning event loop.
`send` checks and appends without yielding. `drain` is nonblocking, preserves FIFO
order, and returns at most 16 messages within a serialized JSON character budget.
If the first message cannot fit, it returns an explicit error without consuming
it; the caller can request a larger budget. `receive` optionally waits for at most
30 seconds. Cancellation leaves future deliveries available. `close` discards
pending data, removes known-secret filter values, and wakes all waiting readers.
The broker is not thread-safe and is not shared between processes or plans.

Messages have four advisory kinds: `status`, `evidence`, `blocked`, and `question`.
They carry a broker-issued run ID and sequence number, and are always marked
untrusted. A `blocked` notice does not block another task automatically; a
`question` does not approve a tool. No message changes permissions, file scope,
model selection, budgets, or the human approval callback. The broker writes no
files, opens no network connection, and starts no subprocess.

Before enqueueing, common credential assignments, bearer headers, common token
prefixes, and private-key blocks are filtered. The host may additionally supply
up to 64 known secret values. Filtering is deliberately described as best effort:
arbitrary encoded secrets or sensitive business information are not detectable
by these patterns. Avoid sending unrelated context in messages.

## Host integration

1. Create one `PlanMailbox([task.task_id for task in plan.tasks])` only after
   validating the plan, and attach it as `registry.team_mailbox`.
2. Register `TeamMessageTool(registry)` and grant `ToolName.TEAM_MESSAGE` only to
   the intended participant roles. Registration alone must not widen other
   tools' permissions. Plan-only and independently scoped security flows need
   not receive messaging rights.
3. Bind both `task_id` and `team_run_id = broker.run_id` in the engine-owned task
   context for each scheduled worker. Reject model-supplied underscore arguments
   before injecting `_caller_task_id` and `_caller_run_id` into this tool.
4. The tool checks both identities against the current broker. This prevents
   sender spoofing and late calls from an old plan with repeated task IDs. A
   `read` call can access only the caller's inbox; participant IDs are returned
   so it can address a sibling. Incoming text remains untrusted reference data.
5. Close the broker in the plan's `finally` block, including cancellation and
   failure. Do not automatically replay successful sends or consumed reads after
   checkpoint recovery; queue operations are marked non-retry-safe. Messages
   are intentionally ephemeral, so durable facts belong in existing task memory
   or handoff/checkpoint records.

At this primitive's delivery, parent integration into the coordinator, registry,
and agent turn is a separate change. The existence of these modules alone does
not establish end-to-end actor messaging in running agents.

## Verification

`PYTHONPATH=src:/tmp/local-coder-merge-agent-deps python -m pytest -q tests/test_team_mailbox.py`
passes **16 tests**. Tests cover two real asynchronous workers exchanging
evidence, same-ID cross-plan isolation, invalid senders/recipients/kinds, explicit
queue and lifetime limits, retained over-budget reads, redaction, close waking
multiple waiters, wait timeout/cancellation, tool identity binding, own-inbox
access, and stale/forged context rejection. They use no provider or external
service. No claim is made about model collaboration quality or reduced token use.

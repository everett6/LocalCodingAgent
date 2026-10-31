# Agent execution and handoff review

Reviewed 2026-10-28 in the current Copilot-completion worktree. Scope: ordinary simple/DAG coding execution, cancellation and recovery boundaries, bounded task handoffs, plus selected BaseAgent, tool-registry, request-context and budget paths. This is not an exhaustive codebase review or a hosted-model benchmark. No model inference, third-party probes, paid services or system configuration changes ran.

## Implemented: host task deadlines

Before the fix, ordinary `_execute_simple` and `_attempt_dag_task` awaited agent execution directly. Unlike delegated, resumed and investigation paths, these paths ignored `AgentTask.timeout_seconds`. An offline fake-agent reproduction with a one-second task deadline returned `COMPLETED` after sleeping 1.2 seconds.

Both ordinary execution paths now enter a host deadline before model routing, repository context, draft preparation and agent execution. On expiry they await cooperative cancellation cleanup and return `BLOCKED` with an explicit recovery requirement and no automatic retry/escalation. Backend `TimeoutError` remains distinguishable from host deadline expiry; caller `CancelledError` propagates unchanged. A component that swallows deadline cancellation cannot turn the expired attempt into a successful response.

A real BaseAgent regression uses a disposable mutating tool that writes one fixture file and blocks. The deadline awaits its cleanup, preserves the pending `started`/`mutation_unknown` journal, detaches the active runtime, and rejects replay from the preceding complete checkpoint. The fixture's partial effect is retained for inspection; the test does not claim rollback.

Cancellation is cooperative. Cleanup may take longer than the configured deadline; this is not an OS-enforced kill guarantee. Queue time before a DAG worker acquires its execution semaphore is outside the attempt deadline. Existing planning/exploration and other phase-specific timeout behavior was not redesigned.

## Implemented: bounded child-result handoffs

The DAG previously returned only task IDs/statuses from `TaskDAG.get_summary`, with empty file, check, issue and discovery lists. Child observations therefore disappeared before review and the final report.

The parent response now preserves deduplicated child files, checks, discoveries, issues and follow-up requirements, plus bounded child summaries labeled as untrusted observations. The complete summary stays below 16,000 characters; individual child summaries are limited to 2,000 characters. Files/check IDs are never shortened into invented paths. Structured lists have count and character ceilings, and truncation is disclosed with a reference to individual task records. Failed children retain their failure evidence. Successful tasks without checks explicitly say that no checks were recorded and do not establish verification.

The legacy `tests_passed` boolean remains compatible for successful no-check tasks. Consumers must continue checking `tests_run` and revision-qualified evidence to distinguish unverified completion from verification. Child observations do not change scope, permission, ownership or approval requirements.

## Validation

`PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_task_deadlines.py tests/test_plan_result_aggregation.py tests/test_coordinator.py tests/test_blocked_orchestration.py tests/test_harness_recovery.py tests/test_process_cancellation.py`

**71 passed**. This includes 14 deadline regressions and three aggregation regressions, existing orchestration/refusal tests, real checkpoint recovery, and process cancellation coverage. New deadline fixtures cover routing/context/draft/agent stalls, no escalation, external cancellation, backend-timeout classification, suppressed cancellation, and unknown mutation journals. Whitespace checks pass for the changed files. Parent integration runs the full suite separately.

## Deferred observation

`BaseAgent.metrics.model_calls` counts completed protocol rounds, whereas the shared budget ledger counts admitted attempts. A flaky model that fails twice before succeeding makes three attempts but reports one completed model round. Recovery validation deliberately binds that metric to iterations plus summaries, so changing its meaning directly would break checkpoint invariants. A future additive attempts metric should retain the existing round count and use the budget ledger as the authoritative spending record. Generation failure wording also uses the configured retry maximum when a budget error stops the first attempt. These are reporting issues, not a demonstrated budget bypass; no change to those counters was made here.

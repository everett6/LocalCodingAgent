# Task 1 coordinator/goal subset report

Owned for this follow-up: `orchestrator/coordinator.py`, `memory/goals.py`, new `tests/test_orchestration_contracts_50.py`. Parent additionally authorized updating the existing goal test to supply explicit host acceptance qualification. BaseAgent, registry, runtime and finding changes remain parent-owned.

## Implemented and verified

| Item | Changes | Evidence |
| --- | --- | --- |
| 1: plan fidelity | Actual `run()` single-task dispatch carries the complete planned AgentTask through a host ContextVar wrapper; `_execute_simple(request, exploration, task_id=None)` retains its public signature. Planned objective, role, files, constraints, success criteria, dependencies, model selection and context are copied rather than reconstructed as a generic coder task. Exploration enriches context without rewriting the objective. A standalone task with unresolved dependencies fails explicitly. | Actual `run()` regression captures planned tester execution and explicit selected model, checks exact task fields, and proves the original plan context remains unchanged. Existing coordinator/session/blocked entrypoint regressions pass. |
| 6: enforced scopes | For actual concurrent DAG waves, host canonical absolute path tuples travel through a ContextVar to `agent.host_write_scope`; `agent.host_parallel_worker` marks the parallel boundary. Empty scope means no file writes. Serialized waves keep `None`. No model JSON grants these attributes. Parent integrated BaseAgent propagation, registry enforcement and checkpoint persistence. | Genuine `_execute_plan` → CoderAgent → ToolRegistry integration rejects an alias path to a sibling's file and an unconfined shell tool call while allowing that sibling's own file write. Separate regression captures canonical host attributes. |
| 8: acceptance evidence | Goal results expose one acceptance record per criterion line, with `unresolved`, `passed` or `stale` status. Generic passing tests do not qualify arbitrary freeform criteria. `finish(..., acceptance_checks={criterion: [observed_check_name]})` accepts an explicit host mapping only when referenced names occur in the host response's observed checks. Model summary alone creates no evidence and never completes a goal. | Regressions cover unresolved criterion despite passing generic tests, explicit qualification, rejection of unobserved check names, and a completed model summary with no observed checks. The old parent criterion test now provides an explicit host mapping as authorized. |
| 13: leaf cancellation subset | Each `run()` owns a request-local set of leaf execution asyncio tasks. It cancels and awaits remaining children in `finally`; leaf timeout/cancellation also removes ownership. Existing DAG sibling cancellation remains intact. | Cancelling a real Coordinator run with a waiting leaf callback executes the child's cleanup and leaves no owned child tasks. Broader blocked/quota/session cancellation-related checks remain green. |
| 32: leaf contracts | Existing depth-one, capability-subset, project path, fan-out and shared-budget guards remain. Children receive a canonical inherited write scope, bounded inherited constraints/criteria, an explicit summary/artifact/check/unresolved-criteria contract, and existing per-task timeout. New optional task-context keys `constraints` and `success_criteria` were communicated to parent for BaseAgent propagation. | Child cancellation integration and existing coordinator tests cover lifecycle; capability/depth/scope enforcement remains the existing registry/tool responsibility. No independent child budget or model-chosen scope was introduced. |
| 33: honest accounting subset | Start events include bounded objective/files, timeout, completion contract and shared-budget identity. Finish events include host AgentMetrics, tracked artifact paths and observed checks. Usefulness and duplicated-context measures are explicitly `None` rather than invented benefits. | Values come from actual AgentResponse host metrics/paths/checks; unknown measurements are labeled unknown. No throughput or quality improvement is claimed. |
| 34: milestone invalidation | Each recorded evidence update has a generation and fingerprints of dependency/child evidence. Currentness checks recurse across dependencies/children and reject changed upstream proof even at an unchanged workspace revision. Downstream stages must each be reverified. Legacy unqualified criteria cannot serve as current dependency proof. | Three-stage regression completes all goals, re-verifies the upstream goal at the same source revision, detects both downstream proofs as stale, then proves reverifying the middle stage alone does not refresh the last stage. Existing parent/child refresh and workspace invalidation tests pass. |

Direct `_run_tests` additionally emits `verification_result` only after awaiting its actual tool result. Data is `passed=result.success`, `command="run_tests({})"`, `tool="run_tests"`, `arguments={}`, `source="tool_result"`, and timezone-aware UTC `recorded_at`. This describes the exact executed tool invocation; it does not fabricate unavailable subprocess argv. UI worker and parent were notified. Two real subprocess regressions exercise passing and failing test suites without model access and verify event/result agreement.

Unavailable simple/DAG agents and unavailable testers now return failed/unverified outcomes instead of synthetic success. A tester response without host-recorded checks also remains unverified; the unavailable-tester regression confirms no fictional fallback check is recorded.

## Verification

The first five focused regressions failed before implementation, covering lost single-task contract, absent parallel host attributes, absent verification event, missing transitive invalidation, and false freeform-criterion completion.

Final command:

```bash
PYTHONPATH=src:/tmp/local-coder-merge-agent-deps python -m pytest -q \
  tests/test_orchestration_contracts_50.py tests/test_coordinator.py \
  tests/test_goals.py tests/test_sessions.py tests/test_blocked_orchestration.py \
  tests/test_quota_integration.py tests/test_coordinator_evaluation.py \
  tests/test_test_runner.py --tb=short
```

Result: **149 passed, 1 skipped in 7.97 seconds**, including eleven focused new cases/parametrized variants. The existing Go integration test skips because Go is unavailable. Owned-file `git diff --check` passed. Earlier transient shared BaseAgent `_handoff` integration failures were communicated and resolved by the parent before the final run. No commits, installs, provider requests or full-suite runs were made.

## Remaining limits and parent-owned work

- This report covers the delegated subset, not all fifteen Task 1 items. Engine ownership, crash-boundary replay, investigation board, no-op recovery/escalation, handoffs and comprehensive approval/queued-operation cancellation remain parent-owned.
- Host mapping qualifies a criterion to actual recorded check names; it does not automatically prove semantic test coverage for arbitrary prose. Criteria stay unresolved without explicit trusted qualification. The model cannot supply this mapping through goal-update fields.
- Leaf accounting records observations but does not yet measure duplicated-context cost or useful-artifact benefit. These fields intentionally remain unknown.
- Leaf constraints/criteria are bounded to 20 inherited entries of 1,000 characters. Larger parent contracts require parent review rather than a claim that this subset measures their full fidelity.
- The direct verification event identifies exact tool options, since the existing RunTestsTool result does not expose exact subprocess argv. Restored checkpoint results do not emit a new observed verification event.
- Final aggregate validation and review of parent BaseAgent/registry integration remain parent-owned. BaseAgent should pass planned `task.depends_on` into GoalLedger creation to persist actual plan dependency relationships; this hook was communicated separately.

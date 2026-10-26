# Adaptive scaffolding

## Paired self-improvement cycles

`improvement_cycle(store, candidates, evaluator)` evaluates the current active
overlay and one to three declarative candidate patches against the same fixed
host-owned benchmark. Each evaluation has a 30-second timeout by default (at most
60 seconds). Candidates do not build on one another. The evaluator must be async,
inherit the request's scope and budget, and return actual `cases`, `passed`,
`total_tokens` and `model_calls` measurements plus a boolean `success`.

A candidate qualifies only when case counts match, passed cases do not decrease,
neither token use nor model calls increases, and at least one measure improves.
Zero-case, malformed, nonfinite and failed-baseline results cannot validate a
revision. Baseline measurements and candidate results are persisted with the
existing private revision history. Timeouts leave candidates inactive.

The loop saves qualifying revisions; it never activates them. Review the exact
revision and its benchmark evidence before using the existing activation action.
Later task outcomes remain observations, and rollback uses the existing approved
revision lifecycle. Passing a benchmark measures that case set only.

Host integration can pass `improvement_evaluator=` to `create_tool_registry` or
`HarnessEvolveTool`, then call `harness_evolve(action="improve", candidates=[...])`.
The evaluator is a trusted callable supplied by the application, never executable
model text. The default application does not configure a task benchmark; the
action reports that prerequisite instead of treating the existing contract smoke
check as proof of improvement. Ordinary proposal/validation remains available.

```python
from local_coder.harness.improvement import improvement_cycle

# evaluate_on_fixed_cases must measure the same authorized cases for each overlay.
report = await improvement_cycle(store, [
    {"strategy": "small_steps"},
    {"strategy": "hypothesis_then_test", "max_strategy_attempts": 2},
], evaluate_on_fixed_cases)
```

## Existing scaffold controls

The harness can propose instructions, skill text, Python helpers and declarative loop plans. These proposals never rewrite engine code. Existing permissions, workspace scope, approval, checkpoints and request budgets remain engine controls outside the evolving scaffold.

`resolve_profile(model_config, role, overrides=None)` selects short instructions for the requested model family and role. It recognizes DeepSeek, GLM, Kimi and Qwen identifiers, including OpenRouter model IDs, and falls back to generic instructions. Explicit controls can change instructions, preferred existing tools, a tool allowlist, approved middleware, strategy and a smaller strategy-attempt limit. A profile only intersects/reorders the schemas supplied by the engine; naming an unavailable tool grants nothing. Unknown fields, arbitrary middleware and unbounded retry limits are rejected.

Approved middleware names are `context_retrieval`, `strategy_review` and `verification`. Approved strategies are `inspect_then_edit`, `hypothesis_then_test` and `small_steps`. Their ordering and selection are declarative inputs to the existing execution loop. They cannot disable the immutable gates or increase configured strategy budgets.

## Revision lifecycle

`EvolutionStore(project_root)` uses private, owner-only records under `.local-coder/harness/`. Revision payloads are immutable; lifecycle events and measured outcomes are separate append-only records. Source is AST-parsed, size-limited and stored as data under `tools/`. Symlinks, unsafe names and nonprivate records are rejected.

1. `harness_evolve(action="propose", kind=..., payload=...)` creates a candidate. Below tier 4, a human approval callback must approve the exact payload, including generated source. Tiers 4 and 5 may propose autonomously.
2. `harness_evolve(action="validate", revision_id=...)` calls an evaluator injected by the application. It must return a real local result with `success` and nonempty numeric `measurements`. A model-supplied `tests_passed` claim never validates a revision. Without an evaluator, validation fails closed.
3. `harness_evolve(action="activate", revision_id=...)` requires passing validation of the unchanged revision and approval according to the current tier. A failed evaluation prevents activation even at tier 5.
4. `harness_evolve(action="rollback", revision_id=...)` reactivates a previously active, still validated revision under the same approval rules. History retains both versions and all events.

`history` exposes revision history; `outcome` records observed behavior for later evaluation. Outcomes are untrusted observations, not instructions or validation decisions. Skill proposals remain text data and are never automatically inserted into the system prompt. Active instruction and loop-plan revisions are available through `active_overlay()` for the application to resolve each turn.

Example instruction proposal:

```json
{"action":"propose","kind":"instructions","payload":{"text":"Before changing a parser, inspect its caller and run a focused boundary-case check."}}
```

Example loop-plan proposal:

```json
{"action":"propose","kind":"loop_plan","payload":{"middleware":["context_retrieval","strategy_review","verification"],"strategy":"small_steps","preferred_tools":["code_search","read_file","edit_file","run_tests"],"max_strategy_attempts":2}}
```

## Generated helpers and delegation

`generated_tool(name=..., input=..., timeout=...)` invokes only an active validated helper. It requires an injected asynchronous isolated runner; there is no host Python execution fallback. The runner receives source, bounded JSON input, a maximum 30-second timeout and the helper name. The application owns sandbox isolation, cancellation, inherited scope and shared budget accounting. AST parsing verifies syntax; it does not make arbitrary Python safe.

`delegate_task(role=..., objective=..., files=[...])` uses the application's scoped callback. The tool accepts at most three child requests before an engine reset, limits children to depth one, rejects parent-relative paths, and refuses roles with capabilities outside the parent's baseline permissions. The callback must also intersect the application's configured permissions, inherit the parent's exact scope and share its existing request budget. Child tasks receive no child allowance. Missing callbacks fail closed.

These APIs are internal application integrations: validators, approval callbacks and sandbox runners must be configured by trusted application code. Ordinary model file-edit and command tools must protect the harness state directory against direct mutation. Owner-only permissions protect other users, but do not isolate tools running as the same operating-system user. Sandbox integrations must not expose the lifecycle state for mutation.

## Application integration and limits

The coordinator binds the tools to a shared `BudgetLedger`. Every model generation,
including retries, summaries, drafts and leaf requests, reserves a conservative
UTF-8 prompt/schema bound plus its output allowance before being sent. Known usage
settles the reservation; unknown/failed requests retain it. A dollar cap requires
explicit input/output prices for every selected model. These are local accounting
limits, not a provider billing guarantee. Small model windows receive a bounded,
objective-ranked tool catalog. Profiles can narrow capabilities further.

The execution loop loads active profile revisions on every step and records task
outcomes afterwards. Retrieval, recent-strategy review and verification-state
reminders are selectable middleware; recovery strategies change the actual
bounded retry guidance. A local evaluator checks overlay contracts or runs generated
helpers in isolation. A passing smoke/contract check establishes usability, not
better benchmark performance. This is an original implementation informed by
[Harness²](https://github.com/google-research/harness2), not its benchmark or an
unbounded optimizer that rewrites the engine.

The new leaf callback allows three read-only children per top-level request at
depth one, enforces each path on every call and shares the parent's ledger. LSP
reference expansion and automatic cross-task memory retrieval are unavailable to
leaves so they cannot expose files beyond the inherited scope.

Before `write_file`, `edit_file` and unified `apply_patch` operations, private
snapshots capture the affected files. Snapshot restoration verifies that current
contents still match the agent's recorded result and refuses to overwrite later
user edits. The app shows before/current views; the IDE uses immutable native diff
documents. Generic file-edit tools cannot write `.git` or `.local-coder` state.

Generated helpers have no host execution fallback. Linux bubblewrap requires
libseccomp and prlimit, denies child-process creation and sockets, seals the
filesystem read-only, and bounds CPU/address space/output. The Docker alternative
uses an already-installed image with no network, memory/process caps and bounded
scratch space; it never pulls an image automatically. **Check isolated runtime**
runs one fixed benign helper. A packaged Linux engine uses an installed system
Python rather than attempting to execute its application binary as an interpreter.

Existing shell/build/test tools still execute trusted project commands on the host
under the existing permission/approval rules. They are not the generated-helper
sandbox, and same-user host commands are not a security boundary protecting engine
state. No claim of complete host-command isolation is made.

Human decisions enter through the authenticated workbench approval inbox; no new
model tool can answer an approval. Expiry, cancellation and oversized unreviewable
requests deny by default. Tiers 1–3 need confirmation for local PoC generation;
tiers 4–5 permit that bounded generation automatically. MCP mutations retain their
own explicit human-approval requirement across tiers.

Paired improvement cycles snapshot candidate controls and evaluator results so
mutable inputs cannot change the baseline during a run. An ordinary evaluator
failure records the candidate error and continues to the next bounded candidate;
cancellation stops the cycle. Comparisons require the same measurement keys and
case count, nondecreasing passes, and nonincreasing tokens and model calls, with
at least one improvement. Additional numeric observations must remain unchanged
because they have no declared optimization direction. A failed baseline stops
evaluation, and successful candidates still require separate activation.

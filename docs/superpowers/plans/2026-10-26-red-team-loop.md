# Evidence-led red-team loop implementation plan

Goal: Improve repository-local investigation continuity and truthful completion.
Architecture: Keep the existing role capability boundary and generic budgeted loop;
add bounded engagement snapshots and a default inert completion hook. Prefer this
to expanding tools or replacing orchestration, which would change authorization.

Constraints: No new attack capabilities, external target activity, dependencies,
model calls during tests, refusal retries, scope changes or mutation replay.
Preserve primary checkout changes. Reuse current managed worktree and PR.

- [x] Bind source findings to matching current evidence and expose stale/unknown
  source provenance; test unrelated files, modified/deleted files and legacy data.
- [x] Replace full workflow-journal hydration with a bounded snapshot that retains
  scope, checklist, evidence references, gaps and explicit source labels.
- [x] Refresh bounded red-team context each round and after recovery. Reject empty
  investigation success and unresolved checklist completion; permit bounded
  alternative strategies while respecting protected stops and hard limits.
- [x] Detect repeated no-progress investigation cycles, retain checkpoint state,
  and produce a structured handoff with attempts, blockers and unfinished work.
- [ ] Run focused/full suites, package and smoke-test, push and update both apps.

Review focus: source evidence races; old records without hashes; injection in
checklist/evidence data; no-progress state surviving resume; completion feedback
never overriding refusal, cancellation, quota, authorization or budget stops.

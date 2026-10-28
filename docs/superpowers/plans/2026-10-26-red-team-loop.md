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
- [x] Run focused/full suites, package and smoke-test, push and update both apps.

Review focus: source evidence races; old records without hashes; injection in
checklist/evidence data; no-progress state surviving resume; completion feedback
never overriding refusal, cancellation, quota, authorization or budget stops.


Delivery: source commits `42d002d` and `771e573`, pushed to PR #22.
Final validation: 1,464 Python tests passed with 3 optional skips and the existing
TestResult collection warning; all 62 desktop/IDE Node tests passed. PyInstaller,
Linux Electron packaging and authenticated packaged-engine smoke passed, including
engagement reports and freshness gaps. No paid inference or external target
activity was used. Windows/macOS native builds were not run locally.

Both installed Linux apps were updated/reopened. The native IDE extension-host
smoke passed activation, command registration, workspace verification and shared
engine authentication. Both installed engine hashes match the tested build;
root-owned mode4755 Chromium helpers are retained. Rollback files are at
`~/.local/opt/local-coder/973967a/updates/771e573`.

Independent review caught and corrected protected-stop language bypassing source
completion and empty scans surviving new files. Final lifecycle validation also
confirmed no-progress stops persist a blocked goal and a final handoff while
unknown-completion mutation blocks retain their existing exceptional replay guard.

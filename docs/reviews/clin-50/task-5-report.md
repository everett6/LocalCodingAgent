# Task 5 — Incremental UI and research provenance

Implemented items 16, 43, 44, 45 and 47 in the existing Clin layout. Initial focused regression run produced five expected failures and one existing-contract pass; implementation then passed the focused checks below.

## Runtime and UI changes

- **16:** `/status?summary=1` returns at most 200 task summaries, truncation disclosure, observed states and recovery guidance without result/error/event bodies. The journal cache refreshes only when its atomic file signature changes. Legacy `/status` is untouched and retains full saved sessions for IDE callers. Selected `/jobs/:id` retrieval loads reports only when the selected saved revision changes. Inactive event history comes from that selected record; only active selected jobs poll `/events`. File lists load when workspace or file/context picker opens. Workspace statistics and diff load on workspace view opening or explicit Refresh workspace; selected source views skip diff generation. There are no file-walk/diff timers. Inspector states that workspace changes are unloaded until requested.
- **43:** `GET /integrations/search/doctor` checks enabled configuration and credential availability locally. Explicit `POST` for a named server uses existing validated MCP settings to list tools and validate a supported bounded search contract; it sends no query and never changes settings or enables services. Diagnostics distinguish disabled, missing credentials, unverified configuration, unsupported contract, connection failure, and checked connection/tool. “Ready” explicitly does not claim search execution or results were verified. Existing MCP execution approvals remain enforced.
- **44:** Successful search output and project-local EvidenceStore `research_search` records include UTC retrieval timestamp, citation URLs, configured provider/server, tool, supplied tool version and supplied per-document versions. Missing versions remain explicitly unknown. Public query text is not persisted. Citation output remains bounded and retained provenance tracks any trimmed results. A local persistence failure does not disguise an already completed search: output explicitly marks provenance as not saved. Research UI provides an explicit saved-provenance inspector using the existing evidence API.
- **45:** UI labels distinguish active execution, pending human approval, no recently observed activity, blocked/incomplete, completed without structured verification, and verified. A 120-second observation gap says execution may still be ongoing, rather than declaring a dead process. Verification never comes from model prose or a generic completed status. The host consumes an ORCHESTRATOR `verification_result` event with a boolean passed result and bounded command; it derives the timestamp, persists evidence in the job final record, and exposes the verified state only on completed jobs with that evidence. An integration test drives this through the real background-job lifecycle.
- **47:** Selected recovery guidance distinguishes a saved agent boundary, new work without a checkpoint, and reconciliation of unknown pending effects. Selection inspects only the named checkpoint's bounded pending marker, without a run-directory walk. A newer pending marker, malformed marker or explicit quota recovery-required notice disables Resume in the UI. Guidance explains that checkpoint continuation does not undo files or resume the enclosing coding workflow.

## Coordinated integration additions

Parent authorized these additions within owned frontend files:

- ContextAccountingPanel now includes an explicit ContextDebugger control calling the context worker's `/context/debugger`. It renders serialized UTF-8 byte attribution, reserved output tokens, provider actual usage, recorded cost/outcome and bounded retrieval selection/omission evidence. It explains tokenization limits and prompt-text retention.
- HarnessPanel renders the release worker's optional `/harness.readiness` item evidence and external gates without turning file presence into acceptance passes.
- FindingsWorkbench sends newline-delimited `negative_control_ids` and `require_negative_controls` (default true) to the existing verify endpoint. Parent owns the matching backend wiring.
- Parent/release owns desktop and IDE route allowlists for GET/POST `/integrations/search/doctor` and GET `/context/debugger`; no host/native files were edited by this task.

## Verification evidence

Commands run from the shared worktree:

```
PYTHONPATH=src:/tmp/local-coder-merge-agent-deps python -m pytest tests/test_ui_research_50.py tests/test_web_search_tool.py tests/test_workbench_api.py tests/test_remote_routes.py -q
# 58 passed
npm --prefix src/local_coder/webui test
# 31 passed
npm --prefix src/local_coder/webui run build
# TypeScript + Vite production build passed
npm --prefix src/local_coder/webui run lint
# oxlint passed
```

Focused coverage includes unchanged legacy status, no idle git calls or unchanged-journal rereads, cache invalidation, selected report retrieval, observed-state proof requirements, live job verification-event persistence, newer pending effect recovery, local credential diagnostics without transport, supported/unsupported MCP schemas, safe connection timeout diagnostics, provenance persistence without query retention, SSR readiness/control rendering, and demand-only workspace request policy.

## Limits and external gates

- Existing storage is a monolithic sessions journal. Initial summary-cache hydration and refresh after a journal change still parse that local file, including prior report bodies; unchanged idle polls perform only a file stat and return bounded summaries. Splitting legacy storage needs a separate migration/ownership decision.
- The UI currently lists the latest 200 summary tasks and discloses truncation; legacy `/status` still exposes the complete journal.
- Runtime/coordinator must emit `verification_result` for real host-observed tests. This task implements and integration-tests the receiving/persistence contract; absent an emitted proof, UI deliberately remains “verification unrecorded.” Parent was notified to coordinate the runtime hook.
- Checkpoint guidance does not assert the complete coordinator validation has already succeeded. The existing resume validator remains authoritative; unknown effects require explicit reconciliation.
- Live third-party MCP servers, credentials, paid inference, model benchmarks, native platform smoke tests, signing/commercial gates and full browser interaction were not exercised. No external service, paid inference, credential, dependency installation, commit or push was performed. Frontend rendering was checked with real React server rendering and production compilation; native/browser smoke remains a parent integration gate.

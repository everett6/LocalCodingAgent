# Clin connection and activity refinement

Checked 2026-10-29 UTC in the isolated Copilot-completion worktree. No installed application or user profile was changed by this UI pass.

## Design and delivered changes

The connection workspace now uses graphite surfaces, quiet blue focus, readable Google Sans and explicit sections for connection, authentication and model choice. The desktop layout places provider and credentials side by side; narrow screens stack them. Model catalog rows distinguish free, metered and unknown prices for the exact ID, retain tool/context/output metadata, and support search, filtering and pagination. Environment variable names remain accessible without competing with the primary masked key field. Providers without a catalog have a direct model-ID field rather than hiding the required field inside Advanced settings.

The conversation activity timeline shows concise observed actions, pairs adjacent matching tool call/results from the same component, and keeps raw source/type/message evidence available by expanding a row. It initially renders the latest twelve steps, with an explicit earlier-history control. Expanded evidence mounts on demand, so long raw diagnostics are absent from the collapsed DOM. Summary labels use bounded Unicode-safe previews; original records remain intact. Browser-tool completion is deliberately not promoted to verified UI status. Failures and budget warnings retain distinct presentation.

Transient connection errors now clear after a successful connection without clearing independent task errors. The local-model view uses serial refreshes, aborts pending browser fetches when closed, ignores status responses predating a model command, and separates recoverable refresh failures from command failures. It does not retry mutations or start models automatically. Unsupported classifier configurations now have an explicit unavailable label.

## Verification

- `npm test`: **49 tests passed**, including new component, activity evidence, pricing/selection, Unicode preview, classifier state and asynchronous polling regressions.
- `npm run build`: TypeScript and Vite production build passed.
- `npm run lint`: oxlint passed.
- `git diff --check -- src/local_coder/webui`: passed.
- Actual CUA browser checks used a disposable local fixture and synthetic catalog/activity. Confirmed settings navigation, model search and free-only filtering, exact selected model state, masked credentials, expandable original activity evidence, and successful reconnect without a stale authentication alert.
- Desktop width 1280 and phone width 390 were inspected. The phone dialog had no horizontal overflow; Save connection remained fully inside the dialog. A clipping issue found by the first screenshot was fixed and rechecked. The temporary viewport override was reset.
- A fixture runtime deliberately delayed status responses by two seconds. Across ten observed UI refreshes, maximum simultaneous status requests remained **one**. Closing the model view stopped further refreshes; two later observations remained at ten with zero active requests. Server teardown made one additional status call. This checks client refresh scheduling, not real runtime/model performance.

Local screenshots are in the worktree scratchpad: `.local-coder/tmp/afk-ui-connections.jpg`, `afk-ui-catalog.jpg`, `afk-ui-mobile.jpg`, `afk-ui-activity.jpg`, and `afk-ui-local-models.jpg`. These are synthetic UI fixtures rather than evidence of a real model run, cloud allowance or security result.

## Limits

No real provider key, paid request, model load, runtime installation, filesystem discovery or security scan was used. Native Windows/macOS execution and production provider connectivity remain outside this pass. Cancellation prevents new client polling and suppresses stale results; an already-started native IPC request or server operation may finish independently. Parent integration must run the full backend/native checks before committing core changes.

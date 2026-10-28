# Model scaffolds, routing and workspace delivery

Verified October 27, 2026 UTC, on the current Linux x64 host.

Implementation commits: `ae43fe8` (model/mode scaffolds, typography, local-model
workspace and slash commands) and `0cbd603` (automatic routing). Both are pushed to
`codex/research-integration`, in [PR #22](https://github.com/everett6/LocalCodingAgent/pull/22).
The PR remains open; this delivery does not merge it.

* Full Python suite: **1,836 passed, 3 skipped**, 8 existing warnings.
* Frontend: **34 tests passed**, TypeScript/Vite production build and oxlint passed.
* Packaged engine smoke: API authentication, frontend asset closure, all bundled
  font/license hashes, the 50-model/94-variant snapshot, three specialist role
  previews, routing settings, context debugger, search diagnostics, readiness,
  tokenizer, offline evaluations, finding/evidence/report/dependency/memory routes.
* Linux PyInstaller engine and Electron 45 unpacked application builds succeeded.
* Actual installed Code OSS extension-host smoke passed: activation, registered
  commands, workspace verification and authenticated shared-engine refresh.
* Installed both app updates from `0cbd603` with matching engine hashes. Manager
  and IDE reopened against the primary workspace and share one authenticated
  installed engine. Manager recorded `window_ready`. Both Chromium helpers retain
  administrator ownership and mode `4755`. A rollback snapshot was retained.
* Installed HTTP checks verified model index counts, all three specialist modes,
  enabled routing, context-debugger privacy and search diagnostics. No classifier
  runtime is currently ready; no weights were downloaded or inference started by
  these checks.

Independent review found and fixed missing optional-context compatibility, basic
tool-catalog omission and potentially expensive economy fallback hops. Regression
tests cover those cases. A pre-existing asynchronous provider test now waits for
worker completion before checking durable terminal state. Stale cached price
records cannot create an automatic economy model.

Browser inspection verified Google Sans rendering, slash-menu behavior, model
selection versus actual loaded state, the local library, model scaffold inspector
and routing controls. UI screenshots are local verification artifacts; model
paths and private workspace state are not published as screenshots in the repo.

The shipped model policies are curated defaults composed with verified identity
and endpoint capability data. Only 32 of the 50 identities have exact OpenRouter
mappings; 18 remain explicitly unmapped. No real-model quality, classifier
calibration, universal-device performance or cost-saving benchmark was run.
Windows/macOS execution and the earlier commercial/signing/readiness limitations
remain unverified. See [routing behavior](automatic-routing.md),
[model workflows](model-harnesses.md), and [source methodology](model-index-sources.md).

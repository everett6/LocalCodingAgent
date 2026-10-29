# Copilot continuation delivery

Checked 2026-10-28. This continues the Copilot work on `codex/test-verified-coding-benchmark` and reconciles it with current `main`. The original checkout and unrelated uncommitted modules remain untouched. The prior benchmark, model comparison, built-in slash workflows, approved lessons and session progress commits are retained.

## Delivered

- Native watchfiles indexing with controlled startup/shutdown, bounded source snapshots, private-path exclusions, stable content generation and stale-snippet rejection. Read/search provenance survives checkpointing; known writes/edits report expected hashes. Rejected file reads cannot poison subsequent checkpoints.
- Multi-file patch preflight, bounded backups/restoration and conservative unknown-mutation handling. Shell Docker/Podman isolation is opt-in, refuses missing runtimes, never pulls images and requests container/process cleanup on timeout/cancellation. Dedicated test/build tools remain outside this setting.
- A fresh bounded mailbox per planned task group, host-bound sender/plan identity, explicit capacity errors, no cross-plan delivery, and cleanup after cancellation/failure. Ordinary DAG ownership, approval and budget controls remain authoritative. Peer messages are transient reference data, not approval or persistent memory.
- An explicitly approved local `verify_ui` tool and optional post-test assertion hook. It produces bounded DOM/runtime/layout observations and private screenshots. Missing support blocks verification. Screenshot capture alone never becomes an assertion pass; vision inference remains `not_run`.
- Guarded per-task Undo, scoped diff views and additional workspace shortcuts integrated into the current Clin shell. Both native bridges admit only the exact Undo POST operation. Undo confirmation uses an accessible in-app dialog rather than a browser-native JavaScript prompt.
- Merge fixes preserve current model routing, quota admission, context fidelity, repository scope, security evidence, task goals, profiles, native UI and recovery behavior. Repeated compaction notices and serialized-output overrun regressions are covered.

## Verification

- Python: **1,996 passed, 3 skipped**; eight existing warnings.
- Frontend: **37 tests passed**, TypeScript/Vite build and oxlint passed.
- Native JavaScript: **31 manager tests and 47 IDE tests passed**.
- Packaged Linux engine: **9 smoke checks passed**: authentication, lightweight status, harness/routing/language/memory routes, compiled asset hashes, actual guarded Undo restoration in a disposable repository, and graceful shutdown.
- Local dependency consistency and whitespace checks pass. Tests use synthetic providers and fixtures; no paid model requests ran.

## Installed Linux check

The user installation was updated to code commit `57ce6c8`, with the manager archive, both packaged engine copies and the IDE bridge verified against the built artifacts. Previous files and installation metadata are retained under the installation's `updates/copilot-57ce6c8` rollback directory. The two root-owned mode-4755 sandbox helpers were preserved, including their inode identities and hashes.

The actual installed IDE extension host passed activation, command registration, workspace verification and authenticated shared-engine refresh using a disposable test profile. Clin and the IDE were then opened on the user's existing project. Clin recorded `engine_ready owned` and `window_ready`; authenticated status, harness, routing, memory and language endpoints returned HTTP 200. No task or model inference was submitted. One initial manager launch exited before engine readiness; direct installed-engine checks and a manager restart succeeded. The initial failure's cause was not established.

The parallel-scope regression now synchronizes sibling completion and reads the sibling file first. This deliberately bypasses the independent unread-overwrite guard, so it consistently tests the host-owned write boundary rather than racing two valid rejection mechanisms.

## Limits

No live Docker/Podman containment, Chromium assertion session, vision model, hosted provider benchmark or Windows/macOS native package was verified. Browser inspection reached the local fixture and exposed a blocking legacy confirmation; embedded browser automation then stalled, so the replacement dialog has component/build coverage rather than a completed browser click-through. Packaged engine Undo was verified independently through authenticated HTTP.

UI verification requires the optional `visual` dependency and compatible installed Chromium; a frozen engine must be rebuilt with that extra. Current artifact storage requires POSIX no-follow directory operations. There is no automatic browser/server installation, vision verdict, neural reranker, guaranteed retrieval latency or universal transactional rollback of external command effects. Background index freshness does not instantaneously rewrite an in-flight model prompt. Broader future roadmap items remain separate work.

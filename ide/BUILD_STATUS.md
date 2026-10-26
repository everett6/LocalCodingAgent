# Verified local build — October 25, 2026

## Passed on Linux x64

- Official stable Code OSS **1.143.1**, commit
  `e646868227c3e3a9e3f24277c8c7220be42c0be7`, is checked out under `upstream/`.
  The reproducible pin is `source/upstream.json`.
- `prepare:source` and repeated `prepare.mjs --check` pass. Fork-managed source
  includes product identity/icons, vendor extension/default exclusions, a native
  Local Coder onboarding replacement, and account-service initialization without
  a hosted chat agent. Upstream license and third-party notices are preserved.
- Full upstream dependency installation succeeded under **Node 24.18.0**.
  Missing Linux development headers were extracted locally under ignored
  `.build/sysroot`; system packages were not changed.
- Full Code OSS client compilation passed with **zero compiler/typecheck errors**.
- Native packaging (`vscode-linux-x64`) passed. The unsigned application is:
  `ide/VSCode-linux-x64/local-coder-ide` (about 1.2 GB with bundled dependencies).
- A real graphical Electron extension-host smoke passed under Xvfb against the
  compiled development fork and the **native packaged fork's bundled extension**.
  It verified activation, command registration, trusted workspace validation,
  and authenticated shared-engine API calls. The extension host exited with 0.
- **17 Node tests** pass under Node 26.5.0 and Node 24.18.0. Coverage includes native
  command/provider/profile/SecretStorage integration, investigation/cancellation,
  untrusted workspace rejection, graceful owned shutdown, shared-engine ownership,
  and fallback cleanup of a descendant in its own process group ignoring SIGTERM.
- The final packaged Python engine passes real authenticated `/status` and
  `/settings` requests in an isolated workspace and exits with code 0 after
  authenticated `/shutdown`, without a termination signal.
- The parent `dist/local-coder-engine`, `extension/bin`, prepared source extension,
  intermediate packaged extension, and final native application copies all have
  the same SHA-256:
  `852584546188c1f09798576bce80f3619b68c6b6af36bbc080aadd106517ade6`.

Ignored local logs are in `.build/compile.log`, `.build/package.log`,
`.build/vscode-smoke.log`, and `.build/tests-node24.log`.

## Release limits

Windows and macOS native applications were **not built or run** here. Build and
launch wrappers cover their native upstream targets, but signing, notarization,
and installers are not configured. A daily stable-source update PR workflow,
Linux x64 native release workflow, and user-configurable native fork updater are now included;
the workflow has not yet been run remotely or published its first fork release.

The pinned upstream npm installation reported audit findings (root dependency
report: 36 findings, including one critical). This fork does not silently rewrite
upstream lockfiles; dependency triage is still needed for public distribution.
Current Code OSS core includes upstream agent SDKs. Its dormant chat initialization
can log a nonfatal `No default agent contributed` diagnostic with no hosted agent
configured; the Local Coder activity bar and extension-host checks pass. The fork
does not claim to remove every upstream agent UI or SDK module.

Tests and smoke checks made no paid model calls. Provider account availability,
billing, model IDs, and production model responses were not validated here.

## Launcher and fork updater verification

The installed native application launched successfully both with a fresh profile
and the normal profile on the actual Wayland graphical session. A graphical
extension-host smoke then passed twice against the rebuilt native package using
`LOCAL_CODER_SMOKE_NATIVE=1`, without forcing X11 or disabling the GPU. These runs
verified real extension activation, commands and authenticated shared-engine
refresh. The reported earlier inability to open the IDE was not reproduced.

The manager previously discarded all native startup output and reported success
as soon as a process spawned. It now detects nonzero exits during the first three
seconds and stores a private startup log. It also discovers a sibling installed
fork without requiring an environment override. Two launcher regressions and all
seven desktop tests pass. The IDE's 17 tests include OpenRouter harness/context
payloads, rejecting external update asset URLs, checksum rejection preserving the
original app, atomic installation, and retention of the previous app folder.

The full native fork packaging command passed again (1.35 minutes). The final
activation manifest adds startup activation and was synchronized as an extension
resource after packaging, followed by the second passing native smoke. The
upstream checker queried the official stable release API and confirmed the pin
remains 1.143.1. The update workflow parses as YAML, but its remote Actions build,
GitHub PR permissions and release publication remain to be verified after push.
No model downloads or inference calls were made during these checks.

## Harness upgrade verification

306 backend tests, 17 IDE tests and 7 desktop tests pass. The manager interface
build and lint pass. A real packaged-engine exercise used a synthetic loopback
model to run a scoped scan, receive model-visible evidence identifiers, record
an unverified finding, save a durable report and shut down cleanly. No paid
model call or external target scan was used. The full native fork package was
rebuilt with the matching engine, then its bundled extension resources were
synchronized after the automatic-update preference change.

Automatic fork installation defaults to enabled on Linux x64 and can be disabled
in user settings. Repository scheduled checks and release publication require
the workflow to be present on the default branch; this feature branch alone
does not activate the daily schedule. Real-model speedups and Windows/macOS
native operation remain unmeasured.

## Workspace startup repair

An existing unauthenticated development preview held the workspace lease and
prevented both desktop clients from starting another engine. That preview was
stopped. The authenticated engine now publishes an owner-only connection record;
the manager and standalone IDE verify and reuse it, including when launched
simultaneously. Cross-language tests exercise all three startup orders, assert
one engine process, and verify that closing a borrower preserves its owner.
308 previous backend checks plus the new cross-language check (309 total),
18 IDE checks and 9 desktop checks pass. Only extension/runtime resources changed
inside the existing Code OSS native package; core editor compilation is unchanged.

The repaired installed manager reached both `engine_ready owned` and
`window_ready` milestones on the real project. The installed IDE client then
attached to that live backend, verified the project identity, and detached
without stopping it. Both installed application processes remained running.
The installed native IDE extension-host smoke also passed. The manager records
window-close requests, renderer exits and quit requests for diagnosing any
subsequent closure independently of backend startup.

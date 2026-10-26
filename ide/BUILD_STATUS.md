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
- **15 Node tests** pass under Node 26.5.0 and Node 24.18.0. Coverage includes native
  command/provider/profile/SecretStorage integration, investigation/cancellation,
  untrusted workspace rejection, graceful owned shutdown, shared-engine ownership,
  and fallback cleanup of a descendant in its own process group ignoring SIGTERM.
- The final packaged Python engine passes real authenticated `/status` and
  `/settings` requests in an isolated workspace and exits with code 0 after
  authenticated `/shutdown`, without a termination signal.
- The parent `dist/local-coder-engine`, `extension/bin`, prepared source extension,
  intermediate packaged extension, and final native application copies all have
  the same SHA-256:
  `53dc6fc0d2c702e6a918fac7c933ffe0083d437fda307bfe1711e53c1c3dc7ab`.

Ignored local logs are in `.build/compile.log`, `.build/package.log`,
`.build/vscode-smoke.log`, and `.build/tests-node24.log`.

## Release limits

Windows and macOS native applications were **not built or run** here. Build and
launch wrappers cover their native upstream targets, but signing, notarization,
installers, and auto-update infrastructure are not configured.

The pinned upstream npm installation reported audit findings (root dependency
report: 36 findings, including one critical). This fork does not silently rewrite
upstream lockfiles; dependency triage is still needed for public distribution.
Current Code OSS core includes upstream agent SDKs. Its dormant chat initialization
can log a nonfatal `No default agent contributed` diagnostic with no hosted agent
configured; the Local Coder activity bar and extension-host checks pass. The fork
does not claim to remove every upstream agent UI or SDK module.

Tests and smoke checks made no paid model calls. Provider account availability,
billing, model IDs, and production model responses were not validated here.

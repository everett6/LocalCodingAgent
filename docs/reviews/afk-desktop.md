# Desktop startup reliability

Reviewed 2026-10-29 UTC against `2c74f0f`. No installed application, sandbox helper, system setting or user profile was changed during this AFK block.

## Proven failures and fixes

- Startup stdout parsing remained active after readiness. A second port record could change the connection origin, and enough later stdout could stop a healthy engine. A synthetic child reproduced the failure; the parser now detaches and drains both streams after settling without retaining runtime output.
- Startup stderr previously yielded only an exit number. A bounded 64 KiB scan now recognizes fixed dependency, permission and database-lock error classes. Raw stderr, exception values, module names, paths and secrets are not retained or returned. Unknown failures remain explicitly unknown.
- The manager permits one automatic retry for an otherwise unclassified exit-code-1 startup failure. Authenticated discovery is checked again before spawning. Known dependency/permission errors, signals, timeouts and normal running-task failures do not receive this retry; shutdown during the retry delay prevents another start. This does not diagnose the unexplained initial startup failure from the prior delivery.
- Startup failure uses an asynchronous close dialog and packaged-app repair guidance. Development installation instructions are shown only in development.
- Cancelling the first project picker before a sidecar existed prevented the application from quitting. The shutdown continuation now runs even when there is no engine. A native-main fixture reproduced the invisible-process failure before the fix.

## Validation

The manager suite passes **37 tests**, including six new regression tests. Fixtures exercise actual synthetic subprocess stdout/stderr and cleanup, plus Electron main-process stubs for startup, cancellation and IPC. No model calls run. The first integration Python run passed **2,060 tests**, with three optional skips; final integration counts are recorded separately after the other workstreams finish.

The dialog behavior is covered through Electron API stubs; this report does not claim a completed native click-through or identify the cause of the prior one-off installed engine exit. Structured classifications are diagnostic hints, not permission decisions or guarantees of repair.

The asynchronous dialog follows the [official Electron dialog API](https://www.electronjs.org/docs/latest/api/dialog). Implementation choices and regression evidence come from this repository.

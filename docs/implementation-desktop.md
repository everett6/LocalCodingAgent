# Desktop, providers, and investigation modes

Goal: finish the browser workflow, ship an Electron shell for Windows/macOS/Linux,
support user-supplied Anthropic and OpenAI-compatible credentials, and make security
and bug investigations evidence driven.

Architecture: retain Python/httpx and the existing tool loop. React uses the same
authenticated HTTP API in browsers and Electron. Electron starts a bundled Python
sidecar on loopback with a fresh token and isolated renderer. Credentials supplied
in settings live in server memory; environment variable references support CLI
use without putting secrets in project configuration. No paid API calls in tests.

User authorization: implement the previously identified fixes and desktop/provider
features directly. Preserve existing uncommitted work; no commits or publication.
Use the existing checkout because the unfinished features are untracked here.

Tasks, executed in this session:
- [x] Regression tests and fixes for context selection and restart recovery.
- [x] BYOK configuration, Anthropic Messages adapter, authenticated compatible APIs.
- [x] Bounded workspace scans, honest coverage reporting, investigation workflows.
- [x] API settings, isolated sessions/events, background jobs and cancellation,
      real workspace inspection and packaged static/PWA assets.
- [x] Accessible React workbench: settings, modes, live activity, task checklist,
      session history, diff, results, errors and connection state.
- [x] Electron sidecar lifecycle, Windows locking, packaging and CI on three OSes.
- [x] Run Python/frontend/desktop checks; review and repair material findings.

Validation: synthetic regression tests, fake model tool loop, HTTP transport mocks,
actual built asset serving, frontend build/lint, desktop lifecycle unit tests.
Native installers are built on their matching CI OS; signing requires publisher
credentials. External scanner absence must be explicit, never reported as clean.

UI direction: follow the supplied Antigravity manager reference: neutral dark
canvas, narrow project/conversation sidebar and a small centered composer. Keep
settings and workspace review in separate views; open the real Code OSS fork
from the manager's Open IDE action.

Progress and decisions are recorded below as implementation proceeds.

User steering: the main application must match the supplied Antigravity manager
screenshot (neutral dark canvas, narrow conversation/project sidebar, centered
composer). A separate IDE must be a real Code OSS source fork, with the shared
agent engine bundled as a first-party integration. The former editor-like main
screen has been replaced, not relabeled as a VS Code fork.

Verification: 244 Python tests passed (excluding unrelated trading tests), React
production build and lint passed, and five desktop lifecycle/bridge tests passed.
The native Linux Code OSS source compilation and package build succeeded.
Windows/macOS packaging is configured in CI but has not run on this Linux host.
Provider tests use mocked transports; no paid model accuracy benchmark is claimed.
Recovery continues a saved agent step and reports when enclosing orchestration
requires continuation. Unsafe interrupted tool batches require inspection.

The Linux manager AppImage and Python wheel also build successfully. The freshly
bundled engine passes real authenticated API/static-asset/shutdown smoke checks.

Final IDE verification: 15 Node tests passed on Node 24 and 26, full source
compilation passed, and both development and packaged native graphical
extension-host smoke checks passed. The packaged fork includes the final engine.
See ../ide/BUILD_STATUS.md for upstream audit findings and release limitations.

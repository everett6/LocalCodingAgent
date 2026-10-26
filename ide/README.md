# Local Coder IDE

A source fork of Microsoft's MIT-licensed Code OSS, with Local Coder branding and a
bundled first-party agent extension. The separate Electron conversation manager
opens this IDE for editing, debugging, terminals, source control, and agent tasks.

The source baseline is official stable **1.143.1**, commit
`e646868227c3e3a9e3f24277c8c7220be42c0be7`, published October 22, 2026.
`source/upstream.json` records the immutable source and required Node version.
`upstream/` is a real, locally checked-out Git repository, ignored by the parent
repository; a clean checkout recreates it with `prepare:source`.

## What is integrated

The Local Coder activity bar provides workspace task history, live task status,
results, agent activity, current workspace diff, and cancellation. The command
palette and view menu expose coding, planning, review, security investigations,
and bug hunting. Investigations accept a workspace path and either report
findings or apply fixes and run tests. These actions use the existing Python
coordinator, tools, safety boundaries, checkpoints, and model harness.

**Local Coder: Configure Provider and Harness** supports OpenAI-compatible APIs,
Anthropic Messages, Ollama, and editable GLM 5.3/Qwen 3.8 presets. Preset model IDs
must be enabled on the user's provider account. Endpoint, exact model ID, token
budget, and `generic`, `glm53`, or `qwen38` profile are sent to `POST /settings`.
Keys entered in the native password prompt are saved in VS Code `SecretStorage`
and supplied only by the extension host to the loopback engine. Provider metadata
is saved in extension global state; it contains no pasted key. Environment key
references are also supported. **Forget Saved Provider Key** removes the saved
copy; an already running engine keeps its in-memory key until it closes.

The extension has no webview. Workspace trust is required, virtual/remote
workspaces are unsupported, and the engine executable setting is read only from
user settings. Multiple local folders can each start their own engine; a manager
shared engine must match the chosen folder exactly.

## Source preparation

Use Git, Node **24.18.0 or newer in major 24**, and npm below 13. The exact
baseline is also in `upstream/.nvmrc`. Do not bypass the Node version check.

From this directory:

```sh
npm run prepare:source
npm test
node scripts/prepare.mjs --check
npm run install:upstream
npm run compile
node scripts/build.mjs electron
npm start -- /absolute/path/to/project
```

Preparation verifies upstream HEAD and Node requirements, applies tracked fork
transformations, generates our own application icons for all three operating
systems, and copies `extension/` into the upstream built-in extensions directory.
It is idempotent and refuses to overwrite edits to fork-managed upstream files.
It never resets or cleans the source checkout. Upstream `LICENSE.txt`,
`ThirdPartyNotices.txt`, and dependency licenses remain in the source and native
package. `UPSTREAM_LICENSE.txt` is the original source license retained here.

Our product metadata disables telemetry, omits Microsoft Marketplace, updates,
external webview CDN, vendor recommendations, and the default hosted chat agent. Built-in vendor chat and its terminal CLI are disabled
by default before extension activation. Fork patches also replace the vendor
onboarding service and let the default account service initialize without
hosted-chat account metadata.
No Marketplace VSIX downloads are configured. Local compatible VSIX installation
remains available. The optional upstream Copilot extension is omitted from npm
subdirectory installs and packaged extension compilation; its mandatory
post-package shim is removed. Current Code OSS core itself uses upstream agent
SDK dependencies, which remain in the pinned lockfiles and build. This fork does
not promise removal of every upstream agent UI or SDK module.

## Platform prerequisites

Build the application on its matching operating system. The upstream guide is
[How to Contribute](https://github.com/microsoft/vscode/wiki/How-to-Contribute);
the pinned source's `build/npm/preinstall.ts` enforces its toolchain requirements.

- **Linux:** Python 3, C/C++ compiler, make, pkg-config, and development packages
  for X11, xkbfile, libsecret, GLib/GIO, Kerberos, and their transitive headers.
  Debian/Ubuntu typically require `build-essential`, `pkg-config`, `libx11-dev`,
  `libxkbfile-dev`, `libsecret-1-dev`, `libkrb5-dev`, and `libglib2.0-dev`.
  An active graphical session and Electron runtime libraries are required to run.
- **macOS:** Xcode command-line tools, Python 3, and Node for the target architecture.
  The output is an unsigned `Local Coder IDE.app`.
- **Windows:** supported Visual Studio C++ build tools/Windows SDK, Python 3, and
  Git. Use Node/npm from the matching x64 or arm64 toolchain. The output is an
  unsigned application folder containing `Local Coder IDE.exe`.

The outer build wrapper invokes npm's JavaScript entry point directly and passes
arguments without a command shell, including paths containing spaces on Windows.
`npm run build` uses the upstream native `vscode-<platform>-<arch>` packaging task.
`LOCAL_CODER_BUILD_ARCH` can specify an upstream-supported target architecture;
matching native dependencies are still required.

## Python engine and standalone packaging

Development launch sets `LOCAL_CODER_REPO_ROOT` to the parent checkout. If no
bundled engine or user executable is present, the extension runs
`python3 -m local_coder.desktop_engine` (`python` on Windows). Set
`LOCAL_CODER_PYTHON` to the desired Python executable if needed. Install the parent
Python project into that interpreter first.

For a self-contained native application, build the engine using the parent
project's packaging process and copy its native executable into:

```text
ide/extension/bin/local-coder-engine       # Linux or macOS
ide/extension/bin/local-coder-engine.exe   # Windows
```

Then run `npm run prepare:source` again, followed by `npm run build`. The engine is
included as a built-in extension resource, with executable mode on Unix. Large
native binaries and upstream dependencies remain ignored. A missing engine
causes the packaging wrapper to fail with an actionable message. A standalone
installation can alternatively set `localCoder.engineExecutable` in user settings
to an absolute trusted engine path.

Native output is under `ide/VSCode-linux-x64/`, `ide/VSCode-darwin-arm64/`, or the
matching platform/architecture directory. This task builds an application folder;
installers, code signing, notarization, and auto-update hosting require additional
release configuration.

## Conversation manager launch contract

The manager can run:

```sh
node /absolute/path/to/ide/scripts/launch.mjs /absolute/path/to/workspace
```

The launcher discovers the packaged fork, then a compiled development checkout.
`LOCAL_CODER_IDE_EXECUTABLE` overrides discovery with an absolute native executable
path (on macOS choose `Local Coder IDE.app/Contents/MacOS/Local Coder IDE`). It does
not fall back to another editor or open a hosted website.

To share the manager's exact model/provider, sessions, events, and agent harness,
pass both variables in the child's environment:

```text
LOCAL_CODER_ENGINE_URL=http://127.0.0.1:<ephemeral-port>
LOCAL_CODER_REMOTE_TOKEN=<fresh-manager-token>
```

Only an exact `http://127.0.0.1:port` origin is accepted: no userinfo, non-loopback
host, path, query, or fragment. The extension checks the engine's project root
against the open folder. Every API request uses Bearer authentication. Shared
engines are never terminated by the IDE. The IDE's provider configuration updates
the same shared engine; saved IDE provider preferences are only automatically
restored for an engine the IDE owns.

The launcher adds `--new-window` and a user-data directory keyed by a truncated
SHA-256 hash of the manager token. This forces an independent editor process when
the manager restarts, avoiding an existing Code OSS process retaining an expired
engine token. The token itself is never placed in command-line arguments or
profile names. `LOCAL_CODER_IDE_DATA_HOME` can relocate these manager profiles.
When launching the native binary directly, the manager must use equivalent
`--new-window --user-data-dir <manager-instance-profile>` arguments. The manager
must stop its own shared engine when appropriate.

Without shared-engine variables, the extension creates a random token, starts an
engine per trusted workspace on an ephemeral loopback port, and stops only those
owned children when a folder closes or the extension deactivates. Owned shutdown
first requests authenticated `/shutdown` with a 15-second timeout so the engine
can cancel jobs and reap tools. An unresponsive engine receives bounded process
tree cleanup: detached POSIX groups (including separately grouped descendants),
or awaited Windows `taskkill /T /F`. Closed connections cannot restart an engine
through an outstanding refresh request. Readiness and
errors are bounded; raw child stdout/stderr are never displayed as logs.

## Verification

`npm test` runs dependency-free Node tests for origin/provider/task validation,
authenticated transport, real child-process readiness/failure/shutdown, shared
engine ownership, native extension provider/SecretStorage/task/cancellation
integration, branding/exclusion patches, icon formats, and launch discovery.
`node scripts/prepare.mjs --check` verifies the actual prepared upstream tree.
`npm run smoke` runs the real IDE extension-host activation/command/authentication
smoke against a built fork; on headless Linux use `xvfb-run -a npm run smoke`.
`LOCAL_CODER_SMOKE_DEV=1` chooses the compiled development tree instead of a native
package. `node --check extension/extension.cjs` validates the dependency-free extension
entry point. These tests do not make paid model calls.

Full native build status and any environment-specific blockers from this work
are recorded in `BUILD_STATUS.md`. Passing extension tests alone does not establish
that Windows, macOS, or Linux installers have been built or signed.

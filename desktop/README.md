# Local Coder desktop manager

The main Electron window is the conversation/project manager. The IDE is the
separate Code OSS source fork in `../ide`; the workspace viewer in the manager
is a read-only review surface.

## Development

From the repository root:

```sh
python -m pip install -e '.[dev]'
cd src/local_coder/webui
npm ci
npm run build
cd ../../../desktop
npm ci
npm start
```

Choose a project folder on first launch. Settings supports GLM 5.3, Qwen 3.8,
Anthropic, OpenAI-compatible servers and Ollama. Set the exact model ID and
region-specific endpoint enabled on your provider account. Keys entered in the
manager remain in engine memory; environment variables are another option.
The manager does not store provider keys or access tokens in browser storage.

The engine binds an ephemeral loopback port and uses a fresh random token.
The sandboxed renderer uses a narrow preload bridge. Node integration, new
windows, external navigation, permissions and downloads are disabled in it.
The manager owns the engine and stops its process tree on exit.

`LOCAL_CODER_PYTHON` selects the development Python interpreter.
`LOCAL_CODER_DESKTOP_PROJECT` skips the initial folder picker.

## Open the IDE

Build the Code OSS fork using `../ide/README.md`. **Open IDE** lets you select
its native executable, or set `LOCAL_CODER_IDE_EXECUTABLE`. The manager passes the
workspace, shared loopback engine URL and token through the child environment.
A fresh IDE user-data directory per engine instance prevents an existing Code
OSS process from reusing stale credentials. Keep the manager open while using
its shared engine. The IDE can also start its own engine independently.

## Package

Build on the target OS. At the repository root:

```sh
python -m pip install pyinstaller
python -m PyInstaller --noconfirm desktop/engine.spec
cd desktop
npm ci
npm test
npm run dist
```

The web UI must be built first. PyInstaller includes Python and the application,
so packaged users do not need a separate Python installation. Output is in
`desktop/release`. The workflow in `.github/workflows/desktop.yml` builds Linux
AppImage, Windows NSIS and macOS DMG/ZIP artifacts on their respective runners.
It never publishes them. Distribution signing/notarization requires the
publisher's own certificates and credentials; no signed release is claimed.

## This checkout's verified build

The Linux AppImage is `release/Local Coder-0.1.0.AppImage`; the unpacked
application is `release/linux-unpacked/local-coder-desktop`. The bundled engine
passed authenticated API, built JavaScript asset, and graceful shutdown checks.
Five desktop bridge/lifecycle tests pass. Windows/macOS packages remain subject
to their native CI build and signing; they were not tested on this Linux host.

### Shared workspace startup

The manager and standalone IDE discover an existing authenticated engine for the
same canonical project folder. Only one engine holds the workspace lease; the
other app reuses it after checking its private connection record and authenticated
identity endpoint. Simultaneous launches retry discovery when the other app wins
the lease. Closing an attached app does not terminate the owner's engine.

Connection records live under `~/.local-coder/connections/` with owner-only
permissions, contain an ephemeral local access token, and are removed on clean
shutdown. Stale records are rejected using process liveness, loopback-only origin,
file permissions, workspace identity and authentication. An unauthenticated
browser preview is deliberately not adopted: stop it before opening the desktop
apps on that project. The owner app must remain open while the other app uses its
engine. Manager startup milestones and failures are recorded in its private
`logs/startup.log`, without the connection token.

# Local UI verification: implementation and limits

This increment finishes a practical browser-verification primitive from the
interrupted Copilot work. It inspects an **already running, explicitly selected
local application**. It does not start a development server, install a browser,
click controls, fill forms, submit requests with credentials, call a model, or
claim that a screenshot has passed visual-model review.

## Interfaces

`VisualVerifier(project_root, playwright_factory=None).verify(url, *,
assertions=None, viewport=None, timeout_seconds=15)` returns structured evidence.
The optional factory and HTTP transport are trusted adapter seams for offline
tests; they are absent from the model tool schema.

`VisualVerificationTool(project_root, approval_callback=None, verifier=None)`
exposes `verify_ui` with these model arguments:

```json
{
  "url": "http://127.0.0.1:3000/app",
  "assertions": [{"selector": "h1", "visible": true, "text": "Welcome"}],
  "viewport": {"width": 1280, "height": 800},
  "timeout_seconds": 15
}
```

Every invocation needs a trusted host approval callback returning exactly
`true`. Merely enabling command auto-approval, adding model-supplied `approved`
arguments, or installing Playwright does not grant this tool authority.
Coding and testing roles receive the tool through the normal registry. Explicit
`verification.visual_url` and validated assertions enable its post-test hook;
unavailable or denied checks block verification rather than triggering model retries. The tool marks navigation as mutating and unsafe for automatic
replay. Interrupted navigation with no completed capture retains an unknown
effect marker.

## Observed results

- `verified`: at least one explicit DOM assertion and the observed basic page
  checks passed.
- `captured`: a viewport screenshot and basic page checks succeeded, but no
  explicit UI assertions were supplied. This is not an assertion pass.
- `failed`: an assertion, HTTP/layout/runtime check, capture, or cleanup failed.
- `unavailable`: the optional browser/package/API, or safe artifact storage, is
  unavailable. There is no silent dependency download or weaker browser mode.

The report contains assertion indices and booleans, HTTP status, viewport and
document dimensions, horizontal overflow, bounded error/network counts, elapsed
time, and PNG path/hash/byte count. Console messages, page text, response bodies,
cookies, and exception details are not copied into reports. Text expectations
are evaluated against at most 4,096 rendered characters from the chosen element.
`vision_assessment` is always `not_run`. The screenshot remains available for
separate human or explicitly configured image-capable assessment.

## Boundaries

The initial URL accepts only HTTP(S) with literal `127.0.0.1` or `[::1]`, no
credentials, query, fragment, control characters, alternate numeric address or
hostname. Each browser session uses a fresh context with no shared browser
profile, no granted permissions, no accepted downloads, blocked service workers,
normal TLS verification and Chromium's sandbox enabled. Failure to start that
sandbox does not trigger a `--no-sandbox` fallback.

All intercepted HTTP requests must use the exact selected origin and one of
GET, HEAD or OPTIONS. They are fetched with existing `httpx` transport, environment
proxies disabled and redirects disabled, then fulfilled back to Chromium. No
request is continued through Chromium's HTTP stack. Redirect responses are
blocked instead of relying on route interception of every redirect hop.
Authorization, cookies and forwarding headers are not forwarded; Set-Cookie is
not applied. WebSockets are closed before connection and older Playwright
versions lacking context-level WebSocket routing are rejected before navigation.
Resources are bounded to 200 requests, 8 MiB per response and 64 MiB in aggregate;
encoded responses are rejected instead of accepting decompression expansion.
Cross-origin assets or blocked application API requests can change rendering.
The report records blocked traffic rather than claiming unrestricted app parity.

These are browser request controls, **not OS network isolation**. This feature
does not claim confinement against malicious browser exploits, WebRTC or every
browser-internal transport, nor that navigating an approved local app is free of
application-side effects. Use it only for the repository's trusted local app.
Actual browser resource consumption is not covered by the screenshot byte cap.

Screenshots use random engine-owned filenames under
`.local-coder/artifacts/ui/`, never model-selected paths. POSIX directory
descriptors, no-follow opens, exclusive creation and private modes protect the
write path. The store refuses new captures at 50 artifacts or 128 MiB, keeps an
exclusive nonblocking writer lock, and never deletes older evidence on its own.
The initial implementation reports safe artifact storage unavailable on
platforms without the required directory-descriptor operations. In particular,
this does not claim native Windows support yet. Existing screenshots may contain
private app content; they stay on-device and are not sent to a model.

## Verification evidence

The focused suite uses inert browser adapters and `httpx.MockTransport`, not a
real browser or model. It checks URL and argument bounds, exact-origin method
filters, redirect refusal, credential stripping, resource limits, WebSocket
denial, required approval, missing dependency/API behavior, visible/text
assertions, overflow, private artifact writes, retention limits, cancellation
cleanup and safe diagnostics. Its PNG fixture is explicitly inert test data.

The backend and tool tests were written before implementation and observed
failing for their missing modules. The redirect/resource tests and retention
regression were also observed failing before their respective fixes. Final
focused result after the parent resolved merge conflicts: **35 passed**.
Python compilation and whitespace checks also passed for the owned files.
No browser package, binary, server or model was installed or started during this
subtask. A live Chromium run remains unverified here; the parent can perform
application UI inspection through the approved computer-use runtime separately.

## Primary-source decisions

The implementation uses a fresh context as documented by
[Playwright's Browser API](https://playwright.dev/python/docs/api/class-browser#browser-new-context).
It blocks service workers because they can bypass ordinary request routing,
and installs context-level WebSocket routing before creating a page, following
the [BrowserContext API](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route-web-socket).
Playwright documents that HTTP `route.fetch` can disable redirect following;
this implementation instead uses bounded streaming HTTP transport to avoid
unbounded resource buffering in that path. See the
[Route API](https://playwright.dev/python/docs/api/class-route#route-fetch).
DOM readiness uses explicit locator waits after `domcontentloaded`, rather than
the discouraged `networkidle` testing heuristic in the
[Page API](https://playwright.dev/python/docs/api/class-page#page-goto).

A frozen desktop engine must be built with the optional `visual` extra to include Playwright. Installing it into an unrelated host Python does not add it to an existing frozen executable. Current release smoke verifies ordinary engine/Undo behavior, not live Chromium or vision inference.

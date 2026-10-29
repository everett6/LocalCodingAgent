# Local control-plane request boundaries

Reviewed 2026-10-29 UTC with disposable workspaces and synthetic request handlers. No load test, third-party host or real model request was used.

The HTTP handler previously parsed a short body as valid JSON even when fewer bytes arrived than its declared Content-Length. A synthetic `{}` body with length three reached the mocked settings action. It now returns HTTP 400 before any control action. An expired/disconnected request-body read closes the connection and returns HTTP 408 where possible. Existing response-write handling avoids a second error response on a closed socket.

The parser now accepts UTF-8 JSON with unique object keys and finite numeric values. Duplicate keys, NaN/Infinity, overflowing floating-point literals and excessive nesting fail before dispatch. A bounded lexical nesting check ignores quoted/escaped bracket characters and rejects more than 64 container levels before JSON parsing. The existing 100 KB request allowance remains unchanged.

Each handler sets a 30-second socket inactivity timeout. This bounds idle reads/writes, not total request execution, aggregate connections, slow incremental traffic or model-task duration. It is not a denial-of-service guarantee or a change to the task deadline/recovery contract.

Regression fixtures cover partial bodies, read timeouts, nonfinite/ambiguous/deep JSON and quoted source text. Existing authenticated HTTP, origin/Host validation, assets and remote-route checks remain in the focused test group. Broader integration counts are recorded by the AFK delivery. No installed application or user state was changed during this phase.

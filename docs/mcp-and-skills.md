# MCP connections and on-demand skills

MCP connections are opt-in. A fresh project has no servers and does not connect
to GitHub. The trusted connection settings API can call `MCPManager.configure()`;
the agent-facing `mcp_call` tool can only list and call tools on named, enabled
servers. It cannot add a server, change credentials, install software or approve
its own requests.

## Configuration

Configuration is stored in `.local-coder/integrations/mcp.json` with mode `0600`
under a private integration directory. State writes reject symlinks. Credential
**values** belong in the environment of the local-coder process. Configuration
stores only the names of explicitly selected variables; it does not import Codex,
`gh`, browser or other application credentials.

For the official remote GitHub server:

```json
{
  "servers": {
    "github": {
      "preset": "github",
      "enabled": false
    }
  }
}
```

The preset expands to `https://api.githubcopilot.com/mcp/`, an explicit
`GITHUB_TOKEN` environment reference, read-only GitHub toolset headers, and a
small named allowlist of read operations. Enabling the connection is a separate
human action. If the variable is missing, the connection fails closed. No OAuth
flow or automatic token discovery is implemented.

The endpoint and read-only headers are documented by GitHub in its
[remote server documentation](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md)
and [server configuration documentation](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md).

A user-installed local server can be explicitly configured as follows. Replace
the example paths with an existing executable and server script:

```json
{
  "servers": {
    "local-docs": {
      "transport": "stdio",
      "enabled": true,
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/docs_server.py"],
      "env": {"DOCS_TOKEN": "MY_DOCS_TOKEN"},
      "read_only_tools": ["search_docs"],
      "timeout": 30
    }
  }
}
```

`env` maps child variable names to source variable names. The child receives
only a minimal PATH/LANG plus those explicitly named values; parent credentials,
HOME and proxy settings are not inherited. Executables must be absolute, exist,
and be executable. Shell and environment launcher commands are rejected. The
client never starts a shell or downloads a server. An enabled executable is
trusted code selected by the user; MCP roots are advisory context and do not
sandbox the server's filesystem or network access.

For an HTTP server, use `transport: "http"`, `url`, and optionally `token_env`.
Only `X-MCP-Readonly`, `X-MCP-Toolsets` and `X-MCP-Tools` are accepted as static
headers; credentials must use `token_env`. URLs reject embedded credentials,
queries and fragments. Remote HTTPS is supported; plaintext HTTP is allowed
only for an explicitly configured loopback literal or `localhost`. Other private,
link-local and metadata addresses are rejected, including through DNS aliases.
Connections pin a validated IP address while preserving the original Host and
HTTPS certificate identity. Redirects, proxy environment inheritance and TLS
verification bypasses are disabled.

`read_only_tools` is a human-selected trust declaration, not inferred from names
or remote `readOnlyHint` annotations. Every other tool requires an injected human
approval callback on each call, including under permissive safety tiers. The
approval describes the server, tool and exact JSON arguments with credential
values redacted. A model-supplied `approved` flag cannot grant permission. If
configuration changes while approval is pending, the call must be requested
again. Close existing connections before replacing configuration.

## Protocol and lifecycle

The implementation negotiates MCP protocol versions `2025-03-26`, `2025-06-18`
or `2025-11-25`, initializes a session, sends `notifications/initialized`, follows
bounded `tools/list` pagination and calls `tools/call` using JSON-RPC. Stdio uses
newline-delimited JSON, **not** LSP Content-Length framing. Streamable HTTP
accepts `application/json` or SSE responses and forwards a server-issued
`Mcp-Session-Id`; closing a session attempts HTTP DELETE. Server `roots/list`
receives only the current project root. Sampling, elicitation and other client
capabilities are not enabled. The framing/session behavior follows the
[MCP transport specification](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports).

Discovery has at most 10 pages and 128 tools, 512-character descriptions,
8 KB schemas and 64 KB total metadata. Protocol messages are bounded to 1 MB;
call arguments to 64 KB; per-operation timeouts to 0.05–120 seconds. Metadata
and responses are marked untrusted, and environment credentials/common token
formats are redacted before model-facing output. Remote errors become safe
local diagnostics. Timeouts, malformed messages and cancellation close the
connection and reap stdio process groups. Hosts must also await `manager.close()`
on shutdown. Automatic reconnect/replay, OAuth, legacy HTTP+SSE endpoints,
standalone background SSE subscriptions and resumability are not implemented.

## Skill library

`skill_library` exposes `action: "list"` for short descriptions and
`action: "read", skill: "tdd"` to retrieve one skill. Builtins cover:

- Evidence-led security
- Bug reproduction
- Test-driven development
- Secure code review
- Context hygiene
- Dependency review

Skill bodies are loaded only on demand. They are untrusted reference data and
cannot override role permissions, engagement scope, human approval or other
host controls. Reading a skill does not execute code, install anything or invoke
its instructions automatically.

Trusted host code may save a custom skill with
`SkillLibrary.save(id, title, content, description)`. It stores JSON reference
text under `.local-coder/skills/`, rejects path traversal/symlinks and builtin
overrides, limits the catalog to 50 custom skills and each body to 16,000
characters, and filters common credential formats and credential assignments on
write/read. This is a heuristic filter, not a proof that arbitrary text contains
no sensitive information. The model tool has no save or execute action.

## Verification

`tests/test_mcp_connections.py` exercises real synthetic stdio subprocesses and
local loopback HTTP servers for initialization, pagination, roots, JSON/SSE,
session headers, approval denial, configuration races, credential redaction,
unsafe URLs/commands/headers, malformed responses and process cancellation.
`tests/test_skill_library.py` checks bounded on-demand retrieval, custom skill
storage, secret filtering and rejection of tool-driven writes. No GitHub account
or external MCP endpoint is contacted by these tests.

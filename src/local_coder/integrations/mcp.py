"""Bounded MCP client; configuration and approvals belong to the human host.

Remote descriptions/results are data, never permission grants. Only explicit
configuration can allow a tool without an injected human approval callback.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import os
import re
import signal
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from local_coder.statedir import state_path, write_state_file

MAX_MESSAGE = 1_048_576
MAX_TOOLS = 128
VERSIONS = {"2025-03-26", "2025-06-18", "2025-11-25"}
NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
SECRET = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"
    r"(?i:Bearer)\s+[A-Za-z0-9._~+/-]{8,}=*)"
)
CONFIG_PATH = ".local-coder/integrations/mcp.json"
ApprovalCallback = Callable[[str], Awaitable[bool]]


class MCPError(ValueError):
    """Safe diagnostic that never includes server bodies or credential values."""


def redact_data(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Remove known environment values and common credential forms recursively."""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return SECRET.sub("[redacted]", value)
    if isinstance(value, list):
        return [redact_data(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            redact_data(str(key), secrets): redact_data(item, secrets)
            for key, item in value.items()
        }
    return value


def _url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(c) < 33 for c in value)
    ):
        raise ValueError(
            "MCP URL must be a bounded HTTPS endpoint or explicit loopback HTTP endpoint"
        )
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        local = host == "localhost"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            if (
                re.fullmatch(r"[0-9.:]+", host)
                or "%" in host
                or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
            ):
                raise ValueError
        if address is not None:
            local = address.is_loopback
            if not address.is_global and not local:
                raise ValueError
        if parsed.scheme not in {"http", "https"} or (
            parsed.scheme == "http" and not local
        ):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError(
            "MCP URL rejects credentials, query strings, private addresses and remote plaintext HTTP"
        ) from None
    return value


def _strings(value: Any, limit: int = 128) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(x, str) or len(x) > 2048 or "\x00" in x for x in value)
    ):
        raise ValueError("Expected a bounded list of strings")
    return list(value)


def _validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or set(config) - {"servers", "version"}:
        raise ValueError("MCP configuration must contain servers")
    servers = config.get("servers", {})
    if not isinstance(servers, dict) or len(servers) > 16:
        raise ValueError("At most 16 named MCP servers may be configured")
    result = {}
    allowed = {
        "preset",
        "transport",
        "enabled",
        "command",
        "args",
        "env",
        "url",
        "token_env",
        "headers",
        "read_only_tools",
        "timeout",
    }
    for name, raw in servers.items():
        if (
            not isinstance(name, str)
            or not NAME.fullmatch(name)
            or not isinstance(raw, dict)
            or set(raw) - allowed
        ):
            raise ValueError("Invalid MCP server name or configuration keys")
        server = copy.deepcopy(raw)
        preset = server.get("preset")
        if preset is not None:
            if preset != "github":
                raise ValueError("Unknown MCP preset")
            defaults = {
                "transport": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "token_env": "GITHUB_TOKEN",
                "headers": {
                    "X-MCP-Readonly": "true",
                    "X-MCP-Toolsets": "repos,issues,pull_requests",
                },
                "read_only_tools": [
                    "get_file_contents",
                    "search_code",
                    "search_repositories",
                    "get_me",
                    "issue_read",
                    "pull_request_read",
                ],
            }
            server = {**defaults, **server}
            if server["url"] != defaults["url"] or server["transport"] != "http":
                raise ValueError("GitHub preset requires the official remote endpoint")
        enabled = server.get("enabled", False)
        timeout = server.get("timeout", 30)
        if (
            not isinstance(enabled, bool)
            or isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not 0.05 <= timeout <= 120
        ):
            raise ValueError(
                "MCP enabled must be boolean and timeout between 0.05 and 120 seconds"
            )
        read_only = _strings(server.get("read_only_tools", []))
        if any(not NAME.fullmatch(item) for item in read_only):
            raise ValueError("Invalid read-only tool name")
        clean = {
            "transport": server.get("transport"),
            "enabled": enabled,
            "timeout": timeout,
            "read_only_tools": read_only,
        }
        if preset:
            clean["preset"] = preset
        if clean["transport"] == "stdio":
            if set(server) & {"url", "headers", "token_env"}:
                raise ValueError("Stdio cannot contain HTTP settings")
            command = server.get("command")
            if (
                not isinstance(command, str)
                or not Path(command).is_absolute()
                or not Path(command).is_file()
                or not os.access(command, os.X_OK)
            ):
                raise ValueError("MCP command must be an absolute executable path")
            if Path(command).resolve().name.lower() in {
                "sh",
                "bash",
                "dash",
                "zsh",
                "fish",
                "env",
                "cmd",
                "cmd.exe",
                "powershell",
                "powershell.exe",
                "pwsh",
            }:
                raise ValueError(
                    "MCP shell and environment launcher commands are forbidden"
                )
            args = _strings(server.get("args", []), 64)
            if any(
                SECRET.search(arg)
                or re.search(
                    r"(?i)(?:^|[-_])(?:token|password|secret|api[_-]?key)(?:=|\s|$)",
                    arg,
                )
                for arg in args
            ):
                raise ValueError(
                    "MCP credentials must use environment variable references, never arguments"
                )
            env = server.get("env", {})
            if (
                not isinstance(env, dict)
                or len(env) > 32
                or any(
                    not isinstance(k, str)
                    or not isinstance(v, str)
                    or not ENV_NAME.fullmatch(k)
                    or not ENV_NAME.fullmatch(v)
                    for k, v in env.items()
                )
            ):
                raise ValueError(
                    "MCP env maps variable names to source environment variable names; values are forbidden"
                )
            clean.update(command=command, args=args, env=env)
        elif clean["transport"] == "http":
            if set(server) & {"command", "args", "env"}:
                raise ValueError("HTTP cannot contain subprocess settings")
            clean["url"] = _url(server.get("url"))
            token = server.get("token_env")
            if token is not None and (
                not isinstance(token, str) or not ENV_NAME.fullmatch(token)
            ):
                raise ValueError("token_env must be an environment variable name")
            headers = server.get("headers", {})
            if not isinstance(headers, dict) or len(headers) > 3:
                raise ValueError(
                    "Only bounded MCP tool selection headers are supported"
                )
            normalized = {
                "x-mcp-readonly": "X-MCP-Readonly",
                "x-mcp-toolsets": "X-MCP-Toolsets",
                "x-mcp-tools": "X-MCP-Tools",
            }
            safe_headers = {}
            for key, value in headers.items():
                if (
                    not isinstance(key, str)
                    or key.lower() not in normalized
                    or not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z0-9_,.-]{1,512}", value)
                ):
                    raise ValueError(
                        "Credentials and arbitrary HTTP headers are forbidden; use token_env"
                    )
                safe_headers[normalized[key.lower()]] = value
            clean.update(token_env=token, headers=safe_headers)
        else:
            raise ValueError("MCP transport must be stdio or http")
        result[name] = clean
    return {"version": 1, "servers": result}


class _Connection:
    def __init__(self, root: Path, config: dict):
        self.root = root
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.client: httpx.AsyncClient | None = None
        self.session: str | None = None
        self.protocol = "2025-03-26"
        self.initialized = False
        self.next_id = 0
        self.request_url = httpx.URL(config.get("url", "http://localhost"))
        self.extensions: dict[str, Any] = {}
        names = list(config.get("env", {}).values()) + [config.get("token_env")]
        self.secrets = tuple(os.environ[n] for n in names if n and os.environ.get(n))

    async def start(self) -> None:
        if self.config["transport"] == "stdio":
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
            for target, source in self.config["env"].items():
                if source not in os.environ:
                    raise MCPError("Configured MCP environment variable is unavailable")
                env[target] = os.environ[source]
            self.process = await asyncio.create_subprocess_exec(
                self.config["command"],
                *self.config["args"],
                cwd=self.root,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_MESSAGE + 1,
                start_new_session=os.name == "posix",
            )
        else:
            token_name = self.config.get("token_env")
            if token_name and (
                not os.environ.get(token_name)
                or any(ord(c) < 33 or ord(c) > 126 for c in os.environ[token_name])
            ):
                raise MCPError(
                    "Configured MCP authentication environment variable is missing or invalid"
                )
            host = self.request_url.host
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host, self.request_url.port, type=socket.SOCK_STREAM
            )
            if not addresses:
                raise MCPError("MCP hostname has no network addresses")
            explicit_loopback = host == "localhost"
            try:
                explicit_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
            for item in addresses:
                address = ipaddress.ip_address(item[4][0])
                if not address.is_global and not (
                    explicit_loopback and address.is_loopback
                ):
                    raise MCPError(
                        "MCP hostname resolves to a forbidden private network address"
                    )
            # Pin this connection to the validated IP; DNS cannot change between
            # validation and I/O. Retain the original HTTPS certificate identity.
            self.request_url = self.request_url.copy_with(host=addresses[0][4][0])
            self.extensions = {"sni_hostname": host}
            self.client = httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=self.config["timeout"]
            )
        result = await self.request(
            "initialize",
            {
                "protocolVersion": self.protocol,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": {"name": "local-coder", "version": "0.1.0"},
            },
        )
        if result.get("protocolVersion") not in VERSIONS or not isinstance(
            result.get("capabilities"), dict
        ):
            raise MCPError("Unsupported MCP initialization response")
        self.protocol = result["protocolVersion"]
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True

    async def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        return await self.send(msg)

    async def _server_message(self, msg: dict, request_id: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            raise MCPError("Malformed MCP JSON-RPC message")
        if "method" in msg:
            if "id" in msg:
                response = {"jsonrpc": "2.0", "id": msg["id"]}
                if msg["method"] == "roots/list":
                    response["result"] = {
                        "roots": [{"uri": self.root.as_uri(), "name": self.root.name}]
                    }
                elif msg["method"] == "ping":
                    response["result"] = {}
                else:
                    response["error"] = {
                        "code": -32601,
                        "message": "Client capability unavailable",
                    }
                await self.send(response)
            return None
        if msg.get("id") != request_id or isinstance(msg.get("id"), bool):
            raise MCPError("MCP response does not match the request")
        if "error" in msg:
            raise MCPError("MCP server returned a protocol error")
        if not isinstance(msg.get("result"), dict):
            raise MCPError("MCP response result must be an object")
        return msg["result"]

    async def send(self, msg: dict) -> dict:
        encoded = json.dumps(msg, allow_nan=False).encode()
        if len(encoded) > MAX_MESSAGE:
            raise MCPError("MCP request exceeds size limit")
        expects_reply = "method" in msg and "id" in msg
        if self.process is not None:
            if self.process.returncode is not None:
                raise MCPError("MCP subprocess exited")
            self.process.stdin.write(encoded + b"\n")
            await self.process.stdin.drain()
            if not expects_reply:
                return {}
            for _ in range(128):
                line = await self.process.stdout.readline()
                if not line or len(line) > MAX_MESSAGE:
                    raise MCPError(
                        "MCP subprocess returned empty or oversized response"
                    )
                try:
                    response = json.loads(line)
                except (ValueError, RecursionError):
                    raise MCPError("MCP subprocess returned malformed JSON") from None
                result = await self._server_message(response, msg["id"])
                if result is not None:
                    return result
            raise MCPError("Too many MCP messages before response")
        return await self._http_send(msg, encoded, expects_reply)

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol,
            **self.config.get("headers", {}),
        }
        headers["Host"] = httpx.URL(self.config["url"]).netloc.decode("ascii")
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        token_name = self.config.get("token_env")
        if token_name:
            # Freeze the exact credential resolved when opening the connection.
            token = self.secrets[-1]
            headers["Authorization"] = "Bearer " + token
        return headers

    async def _http_send(self, msg: dict, encoded: bytes, expects_reply: bool) -> dict:
        if self.client is None:
            raise MCPError("MCP HTTP connection is unavailable")
        async with self.client.stream(
            "POST",
            self.request_url,
            content=encoded,
            headers=self._headers(),
            extensions=self.extensions,
        ) as response:
            if response.status_code not in {200, 202, 204}:
                raise MCPError("MCP HTTP request failed (redirects are disabled)")
            if msg.get("method") == "initialize":
                session = response.headers.get("mcp-session-id")
                if session is not None and (
                    not 1 <= len(session) <= 256
                    or any(not 33 <= ord(c) <= 126 for c in session)
                ):
                    raise MCPError("Invalid MCP session header")
                self.session = session
            if not expects_reply:
                return {}
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if mime not in {"application/json", "text/event-stream"}:
                raise MCPError("Unsupported MCP HTTP response content type")
            buffer = b""
            total = 0
            count = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_MESSAGE:
                    raise MCPError("MCP HTTP response exceeds size limit")
                buffer += chunk
                if mime != "text/event-stream":
                    continue
                buffer = buffer.replace(b"\r\n", b"\n")
                while b"\n\n" in buffer:
                    event, buffer = buffer.split(b"\n\n", 1)
                    data = b"\n".join(
                        line[5:].lstrip(b" ")
                        for line in event.split(b"\n")
                        if line.startswith(b"data:")
                    )
                    if not data:
                        continue
                    count += 1
                    if count > 128:
                        raise MCPError("Too many MCP SSE events")
                    result = await self._server_message(self._decode(data), msg["id"])
                    if result is not None:
                        return result
            if mime == "application/json":
                result = await self._server_message(self._decode(buffer), msg["id"])
                if result is not None:
                    return result
            raise MCPError("MCP stream ended without a response")

    @staticmethod
    def _decode(data: bytes) -> dict:
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            raise MCPError("MCP HTTP server returned malformed JSON") from None

    async def close(self) -> None:
        self.initialized = False
        if self.process is not None:
            try:
                if os.name == "posix":
                    os.killpg(self.process.pid, signal.SIGTERM)
                elif self.process.returncode is None:
                    self.process.terminate()
                await asyncio.wait_for(self.process.wait(), 0.5)
            except (TimeoutError, ProcessLookupError):
                pass
            finally:
                # Children may retain stdout or ignore TERM after the direct
                # server exits. Reap the complete process group, then the parent.
                if os.name == "posix":
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif self.process.returncode is None:
                    self.process.kill()
                await self.process.wait()
        if self.client is not None:
            try:
                if self.session:
                    await asyncio.wait_for(
                        self.client.delete(
                            self.request_url,
                            headers=self._headers(),
                            extensions=self.extensions,
                        ),
                        0.5,
                    )
            except (TimeoutError, httpx.HTTPError):
                pass
            finally:
                await self.client.aclose()
                self.client = None


class MCPManager:
    """Own private connections; tools cannot configure servers or credentials."""

    def __init__(
        self,
        project_root: str | Path,
        approval_callback: ApprovalCallback | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.approval_callback = approval_callback
        self._connections: dict[str, _Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._config = {"version": 1, "servers": {}}
        path = self.project_root / CONFIG_PATH
        if path.exists() or path.is_symlink():
            path = state_path(self.project_root, CONFIG_PATH)
            if path.stat().st_size > 64_000:
                raise ValueError("MCP configuration exceeds size limit")
            self._config = _validate_config(json.loads(path.read_text()))

    def status(self) -> dict:
        servers = copy.deepcopy(self._config["servers"])
        for name, config in servers.items():
            connection = self._connections.get(name)
            config["connected"] = bool(connection and connection.initialized)
            sources = list(config.get("env", {}).values()) + [config.get("token_env")]
            config["credentials_available"] = all(
                bool(os.environ.get(n)) for n in sources if n
            )
        return {"servers": redact_data(servers), "presets": ["github"]}

    def configure(self, config: dict) -> dict:
        if self._connections or any(lock.locked() for lock in self._locks.values()):
            raise ValueError(
                "Close active MCP connections before changing configuration"
            )
        validated = _validate_config(config)
        path = write_state_file(
            self.project_root, CONFIG_PATH, json.dumps(validated, indent=2) + "\n"
        )
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)
        self._config = validated
        return self.status()

    def _server(self, server: str) -> dict:
        if not isinstance(server, str) or server not in self._config["servers"]:
            raise MCPError("Unknown MCP server; configure it in connection settings")
        config = self._config["servers"][server]
        if not config["enabled"]:
            raise MCPError("MCP server is disabled")
        return config

    async def _run(
        self, server: str, operation: Callable[[_Connection], Awaitable[Any]]
    ) -> Any:
        config = self._server(server)
        lock = self._locks.setdefault(server, asyncio.Lock())
        async with lock:
            connection = self._connections.get(server)
            try:
                async with asyncio.timeout(config["timeout"]):
                    if connection is None:
                        connection = _Connection(self.project_root, config)
                        self._connections[server] = connection
                        await connection.start()
                    return await operation(connection)
            except BaseException as exc:
                self._connections.pop(server, None)
                if connection is not None:
                    await asyncio.shield(connection.close())
                if isinstance(
                    exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
                ):
                    raise
                if isinstance(exc, MCPError):
                    raise
                if isinstance(exc, TimeoutError):
                    raise MCPError(
                        "MCP operation timed out; connection closed"
                    ) from None
                raise MCPError("MCP operation failed; connection closed") from None

    async def _list(self, server: str, connection: _Connection) -> list[dict]:
        tools = []
        seen = set()
        cursor = None
        config = self._config["servers"][server]
        for _ in range(10):
            page = await connection.request(
                "tools/list", {"cursor": cursor} if cursor else {}
            )
            entries = page.get("tools")
            if not isinstance(entries, list):
                raise MCPError("MCP tools/list must contain a tool list")
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("name"), str)
                    or not NAME.fullmatch(entry["name"])
                    or entry["name"] in seen
                ):
                    raise MCPError("MCP returned invalid or duplicate tool names")
                if len(tools) >= MAX_TOOLS:
                    raise MCPError("MCP tool catalog exceeds limit")
                seen.add(entry["name"])
                schema = entry.get("inputSchema", {"type": "object"})
                if not isinstance(schema, dict) or len(json.dumps(schema)) > 8192:
                    schema = {
                        "type": "object",
                        "description": "Remote schema omitted because it exceeded bounds",
                    }
                description = entry.get("description", "")
                tools.append(
                    {
                        "name": entry["name"],
                        "description": description[:512]
                        if isinstance(description, str)
                        else "",
                        "inputSchema": schema,
                        "untrusted": True,
                        "requires_approval": entry["name"]
                        not in config["read_only_tools"],
                    }
                )
                if len(json.dumps(tools)) > 64_000:
                    raise MCPError("MCP tool metadata exceeds limit")
            cursor = page.get("nextCursor")
            if cursor is None:
                return redact_data(tools, connection.secrets)
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
                raise MCPError("Invalid MCP pagination cursor")
        raise MCPError("MCP pagination exceeds limit")

    async def list_tools(self, server: str) -> list[dict]:
        return await self._run(
            server, lambda connection: self._list(server, connection)
        )

    async def call_tool(self, server: str, tool: str, arguments: dict) -> dict:
        config = self._server(server)
        if (
            not isinstance(tool, str)
            or not NAME.fullmatch(tool)
            or not isinstance(arguments, dict)
        ):
            raise MCPError("MCP tool name and arguments object are required")
        try:
            encoded = json.dumps(arguments, allow_nan=False)
            if len(encoded.encode()) > 64_000:
                raise ValueError
            arguments = json.loads(encoded)
        except (ValueError, TypeError, RecursionError):
            raise MCPError("MCP arguments must be bounded JSON") from None
        if tool not in config["read_only_tools"]:
            if self.approval_callback is None:
                raise MCPError("Human approval is required for this MCP tool")
            sources = list(config.get("env", {}).values()) + [config.get("token_env")]
            secrets = tuple(os.environ[n] for n in sources if n and os.environ.get(n))
            description = (
                f"MCP tool {server}/{tool} may change external state. Arguments (untrusted): "
                + str(redact_data(encoded, secrets))
            )
            if await self.approval_callback(description) is not True:
                raise MCPError("Human approval denied for MCP tool")
            if self._server(server) is not config:
                raise MCPError(
                    "MCP configuration changed while awaiting approval; request again"
                )

        async def call(connection):
            if self._server(server) is not config:
                raise MCPError(
                    "MCP configuration changed while awaiting approval; request again"
                )
            tools = await self._list(server, connection)
            if not any(item["name"] == tool for item in tools):
                raise MCPError(
                    "Tool is not present in the configured MCP server catalog"
                )
            result = await connection.request(
                "tools/call", {"name": tool, "arguments": arguments}
            )
            return {**redact_data(result, connection.secrets), "untrusted": True}

        return await self._run(server, call)

    async def close(self) -> None:
        connections = list(self._connections.values())
        self._connections.clear()
        for connection in connections:
            await connection.close()

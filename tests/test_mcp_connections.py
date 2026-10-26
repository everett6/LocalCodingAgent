"""MCP integration tests use only synthetic local subprocesses and HTTP servers."""

import asyncio
import json
import os
import socket
import stat
import sys
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

import pytest

from local_coder.integrations.mcp import MCPError, MCPManager
from local_coder.tools.mcp import MCPTool


def run_async(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


@pytest.fixture
def stdio_config(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text("""import json, os, sys, time
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "notifications/initialized":
        initialized = True
        continue
    if "id" not in msg:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        assert initialized
        if not msg.get("params", {}).get("cursor"):
            result = {"tools": [{"name": "read", "description": "untrusted description", "inputSchema": {"type": "object"}}], "nextCursor": "next"}
        else:
            result = {"tools": [{"name": "write", "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        args = msg["params"]["arguments"]
        if args.get("hang"):
            time.sleep(30)
        if args.get("malformed"):
            print("not-json", flush=True)
            continue
        if args.get("roots"):
            print(json.dumps({"jsonrpc": "2.0", "id": "root", "method": "roots/list"}), flush=True)
            roots = json.loads(sys.stdin.readline())["result"]["roots"]
            result = {"content": [{"type": "text", "text": roots[0]["uri"]}]}
        else:
            result = {"content": [{"type": "text", "text": json.dumps({"args": args, "token": os.environ.get("TOKEN"), "unexpected": os.environ.get("UNRELATED_SECRET")})}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
""")
    return {
        "transport": "stdio",
        "enabled": True,
        "command": sys.executable,
        "args": [str(script)],
        "read_only_tools": ["read"],
        "timeout": 2,
    }


def test_explicit_private_configuration_and_disabled_github(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_" + "a" * 40)
    manager = MCPManager(tmp_path)
    assert manager.status()["servers"] == {}
    status = manager.configure({"servers": {"github": {"preset": "github"}}})
    server = status["servers"]["github"]
    assert server["enabled"] is False
    assert server["url"] == "https://api.githubcopilot.com/mcp/"
    assert server["token_env"] == "GITHUB_TOKEN"
    assert server["headers"]["X-MCP-Readonly"] == "true"
    path = tmp_path / ".local-coder/integrations/mcp.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "github_pat_" not in path.read_text() + json.dumps(status)
    assert MCPManager(tmp_path).status()["servers"] == status["servers"]


@pytest.mark.parametrize(
    "bad",
    [
        {"transport": "stdio", "command": "python"},
        {"transport": "stdio", "command": "/bin/sh", "args": ["-c", "echo bad"]},
        {"transport": "http", "url": "http://example.com/mcp"},
        {"transport": "http", "url": "https://user:pass@example.com/mcp"},
        {"transport": "http", "url": "https://example.com/mcp?token=secret"},
        {
            "transport": "http",
            "url": "https://example.com",
            "headers": {"Authorization": "Bearer secret"},
        },
        {
            "transport": "http",
            "url": "https://example.com",
            "headers": {"X-MCP-Tools": "x\r\nAuthorization: secret"},
        },
        {
            "transport": "stdio",
            "command": sys.executable,
            "env": {"TOKEN": "literal token value"},
        },
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["--token", "literal-secret"],
        },
        {"transport": "http", "url": "https://example.com", "enabled": "false"},
    ],
)
def test_rejects_unsafe_or_ambiguous_configuration(tmp_path, bad):
    with pytest.raises(ValueError):
        MCPManager(tmp_path).configure({"servers": {"bad": bad}})


@run_async
async def test_stdio_lifecycle_pagination_roots_and_secret_redaction(
    tmp_path, stdio_config, monkeypatch
):
    monkeypatch.setenv("TEST_MCP_TOKEN", "sensitive-value-to-redact")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-inherit")
    stdio_config["env"] = {"TOKEN": "TEST_MCP_TOKEN"}
    manager = MCPManager(tmp_path)
    manager.configure({"servers": {"fake": stdio_config}})
    tools = await manager.list_tools("fake")
    assert [tool["name"] for tool in tools] == ["read", "write"]
    assert all(tool["untrusted"] is True for tool in tools)
    assert tools[1]["requires_approval"] is True
    result = await manager.call_tool("fake", "read", {"message": "hello"})
    encoded = json.dumps(result)
    assert "hello" in encoded
    assert "[redacted]" in encoded
    assert "must-not-inherit" not in encoded
    assert "sensitive-value-to-redact" not in encoded
    roots = await manager.call_tool("fake", "read", {"roots": True})
    assert tmp_path.as_uri() in json.dumps(roots)
    await manager.close()
    assert manager.status()["servers"]["fake"]["connected"] is False


@run_async
async def test_remote_readonly_hint_never_bypasses_human_approval(
    tmp_path, stdio_config
):
    manager = MCPManager(tmp_path)
    manager.configure({"servers": {"fake": stdio_config}})
    with pytest.raises(MCPError, match="approval"):
        await manager.call_tool("fake", "write", {"approved": True})
    seen = []

    async def approval(description):
        seen.append(description)
        return True

    manager.approval_callback = approval
    assert (await manager.call_tool("fake", "write", {}))["content"]
    assert len(seen) == 1 and "fake" in seen[0] and "write" in seen[0]
    await manager.close()


@run_async
@pytest.mark.parametrize("mode", ["timeout", "cancel", "malformed"])
async def test_stdio_failure_reaps_process(tmp_path, stdio_config, mode):
    stdio_config["timeout"] = 0.15 if mode == "timeout" else 2
    manager = MCPManager(tmp_path)
    manager.configure({"servers": {"fake": stdio_config}})
    await manager.list_tools("fake")
    process = manager._connections["fake"].process
    task = asyncio.create_task(
        manager.call_tool(
            "fake",
            "read",
            {"malformed": mode == "malformed", "hang": mode != "malformed"},
        )
    )
    if mode == "cancel":
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(MCPError):
            await task
    assert process.returncode is not None
    assert not manager.status()["servers"]["fake"]["connected"]
    await manager.close()


@asynccontextmanager
async def http_mcp(*, sse=False, redirect=False):
    requests = []

    async def handle(reader, writer):
        try:
            first = await reader.readline()
            headers = {}
            while line := await reader.readline():
                if line == b"\r\n":
                    break
                name, value = line.decode().split(":", 1)
                headers[name.lower()] = value.strip()
            data = await reader.readexactly(int(headers.get("content-length", "0")))
            msg = json.loads(data) if data else {}
            requests.append((first, headers, msg))
            if redirect:
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/leak\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            if first.startswith(b"DELETE") or "id" not in msg:
                writer.write(
                    b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            method = msg["method"]
            result = (
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "http", "version": "1"},
                }
                if method == "initialize"
                else (
                    {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
                    if method == "tools/list"
                    else {"content": [{"type": "text", "text": "ok"}]}
                )
            )
            body = json.dumps(
                {"jsonrpc": "2.0", "id": msg["id"], "result": result}
            ).encode()
            if sse:
                body = b"event: message\r\ndata: " + body + b"\r\n\r\n"
            mime = b"text/event-stream" if sse else b"application/json"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: "
                + mime
                + b"\r\nMcp-Session-Id: test-session\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
        finally:
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp", requests
    finally:
        server.close()
        await server.wait_closed()


@run_async
@pytest.mark.parametrize("sse", [False, True])
async def test_real_streamable_http_session_json_and_sse(tmp_path, monkeypatch, sse):
    monkeypatch.setenv("MCP_TOKEN", "explicit-http-token")
    async with http_mcp(sse=sse) as (url, requests):
        manager = MCPManager(tmp_path)
        manager.configure(
            {
                "servers": {
                    "http": {
                        "transport": "http",
                        "url": url,
                        "enabled": True,
                        "token_env": "MCP_TOKEN",
                        "read_only_tools": ["read"],
                    }
                }
            }
        )
        assert (await manager.list_tools("http"))[0]["name"] == "read"
        assert (await manager.call_tool("http", "read", {}))["content"][0][
            "text"
        ] == "ok"
        await manager.close()
        assert requests[0][1]["authorization"] == "Bearer explicit-http-token"
        assert all(req[1]["mcp-session-id"] == "test-session" for req in requests[1:])
        assert requests[-1][0].startswith(b"DELETE")


@run_async
async def test_http_never_follows_redirect_and_tool_cannot_configure(tmp_path):
    async with http_mcp(redirect=True) as (url, requests):
        manager = MCPManager(tmp_path)
        manager.configure(
            {"servers": {"http": {"transport": "http", "url": url, "enabled": True}}}
        )
        result = await MCPTool(manager).execute(action="list", server="http")
        assert not result.success and len(requests) == 1
        result = await MCPTool(manager).execute(
            action="configure", server="evil", config={}
        )
        assert not result.success
        await manager.close()


@run_async
async def test_approval_cannot_cross_configuration_revision(tmp_path, stdio_config):
    manager = MCPManager(tmp_path)
    manager.configure({"servers": {"fake": stdio_config}})

    async def approval(description):
        changed = {**stdio_config, "env": {"NEW": "NEW_ENV"}}
        manager.configure({"servers": {"fake": changed}})
        return True

    manager.approval_callback = approval
    with pytest.raises(MCPError, match="configuration changed"):
        await manager.call_tool("fake", "write", {})
    await manager.close()


def test_private_metadata_addresses_and_symlink_config_are_rejected(tmp_path):
    manager = MCPManager(tmp_path)
    for url in [
        "https://169.254.169.254/mcp",
        "https://10.0.0.1/mcp",
        "https://[fe80::1]/mcp",
    ]:
        with pytest.raises(ValueError):
            manager.configure({"servers": {"bad": {"transport": "http", "url": url}}})
    other = tmp_path / "outside.json"
    other.write_text('{"servers": {}}')
    directory = tmp_path / ".local-coder/integrations"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mcp.json").symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        MCPManager(tmp_path)


@run_async
async def test_hostname_resolving_to_private_network_is_denied_before_http(
    tmp_path, monkeypatch
):
    manager = MCPManager(tmp_path)
    manager.configure(
        {
            "servers": {
                "http": {
                    "transport": "http",
                    "url": "https://mcp.example/mcp",
                    "enabled": True,
                }
            }
        }
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("169.254.169.254", 443),
            )
        ],
    )
    with pytest.raises(MCPError, match="network"):
        await manager.list_tools("http")
    await manager.close()


@run_async
async def test_close_kills_descendants_even_when_server_parent_has_exited(tmp_path):
    if os.name != "posix":
        pytest.skip("Process group lifecycle is POSIX-specific")
    script = tmp_path / "orphan.py"
    pid_file = tmp_path / "child.pid"
    script.write_text("""import json, os, subprocess, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc":"2.0", "id":msg["id"], "result":{"protocolVersion":"2025-03-26", "capabilities":{}}}), flush=True)
    elif msg.get("method") == "tools/list":
        child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])
        open(sys.argv[1], "w").write(str(child.pid))
        os._exit(0)
""")
    manager = MCPManager(tmp_path)
    manager.configure(
        {
            "servers": {
                "orphan": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(script), str(pid_file)],
                    "enabled": True,
                    "timeout": 0.2,
                }
            }
        }
    )
    with pytest.raises(MCPError):
        await manager.list_tools("orphan")
    pid = int(pid_file.read_text())
    for _ in range(20):
        proc = Path(f"/proc/{pid}/stat")
        if not proc.exists() or proc.read_text().split()[2] == "Z":
            break
        await asyncio.sleep(0.01)
    else:
        os.kill(pid, 9)
        pytest.fail("MCP descendant survived transport cleanup")
    await manager.close()


@run_async
async def test_positive_approval_is_not_reused_and_denial_never_calls_server(
    tmp_path, stdio_config
):
    decisions = [True, False]

    async def approval(description):
        return decisions.pop(0)

    manager = MCPManager(tmp_path, approval_callback=approval)
    manager.configure({"servers": {"fake": stdio_config}})
    assert await manager.call_tool("fake", "write", {})
    with pytest.raises(MCPError, match="denied"):
        await manager.call_tool("fake", "write", {})
    assert not decisions
    await manager.close()

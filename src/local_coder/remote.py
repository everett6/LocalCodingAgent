"""Local HTTP control plane for remote terminal sessions, and the browser
UI (webui/index.html) served alongside it -- a single-page app that talks
to this same JSON API. It is packaged as an installable PWA so browsers on
Linux, Windows, and macOS can add it to the desktop, taskbar, or dock without
a per-OS native build. See README.md's "Browser UI" section."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import subprocess
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from local_coder import local_server
from local_coder.orchestrator.config_loader import load_config
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.orchestrator.sessions import SessionStore
from local_coder.types import AgentEvent, AgentRole

_WEBUI_INDEX = Path(__file__).parent / "webui" / "index.html"
_WEBUI_ASSETS = {
    "/manifest.webmanifest": (Path(__file__).parent / "webui" / "manifest.webmanifest", "application/manifest+json"),
    "/sw.js": (Path(__file__).parent / "webui" / "sw.js", "application/javascript"),
    "/icon.svg": (Path(__file__).parent / "webui" / "icon.svg", "image/svg+xml"),
}


def _git(project_root: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=project_root, capture_output=True, text=True, timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def workspace_status(project_root: str) -> dict:
    """Real branch + working-tree diff stat for the UI's workspace panel --
    the Terminal/Plan panes used to show canned example text; this is what
    replaced it with live data. Read-only (status/diff/branch only), so it
    needs no approval gate, matching GitStatusTool/GitDiffTool's own
    read-only classification."""
    branch = _git(project_root, "branch", "--show-current").strip() or "(detached)"
    # --numstat against HEAD covers staged and unstaged changes to tracked
    # files in one call; untracked files don't show up in a diff at all, so
    # they're listed separately and reported with a 0/0 stat.
    numstat = _git(project_root, "diff", "--numstat", "HEAD")
    untracked = [
        line for line in _git(project_root, "ls-files", "--others", "--exclude-standard").splitlines() if line
    ]

    files = []
    total_additions = total_deletions = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        additions_s, deletions_s, path = parts
        additions = int(additions_s) if additions_s.isdigit() else 0
        deletions = int(deletions_s) if deletions_s.isdigit() else 0
        total_additions += additions
        total_deletions += deletions
        files.append({"path": path, "additions": additions, "deletions": deletions, "untracked": False})
    for path in untracked:
        files.append({"path": path, "additions": 0, "deletions": 0, "untracked": True})

    return {
        "branch": branch,
        "files": files,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "status": _git(project_root, "status", "-s").strip(),
    }


class RemoteControlServer:
    """Serve read-only status plus explicit plan/run/review actions."""

    def __init__(
        self,
        project_root: str,
        config_path: str | None = None,
        model_name: str | None = None,
        yolo: bool = False,
        token: str | None = None,
    ):
        self.project_root = project_root
        self.config_path = config_path
        self.model_name = model_name
        # The HTTP server has no terminal to prompt with, so there is never
        # an interactive approval callback here -- ASK-risk actions are
        # denied unless the operator explicitly opted into --yolo when
        # starting the server.
        self.yolo = yolo
        self.token = token
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self._event_seq = 0
        self.lock = Lock()
        self.sessions = SessionStore(project_root)

    def _event(self, event: AgentEvent) -> None:
        with self.lock:
            self._event_seq += 1
            payload = event.model_dump(mode="json")
            # A monotonic id, not this deque's own index -- the deque is a
            # fixed-size ring buffer shared across every run this process
            # has served, so old entries fall off the front as new ones
            # arrive. A client polling for "events after N" needs an id
            # that survives that eviction; a raw list index does not.
            payload["seq"] = self._event_seq
            self.events.append(payload)

    def _coordinator(self) -> Coordinator:
        config = load_config(self.config_path, project_root=self.project_root)
        if self.model_name:
            config.agentic.role_models = {role.value: self.model_name for role in AgentRole}
        if self.yolo:
            config.approval.require_approval_for_commands = False
            config.approval.require_approval_for_commits = False
        coordinator = Coordinator(config=config, project_root=self.project_root)
        coordinator.on_event(self._event)
        return coordinator

    def handle(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/status":
            return 200, {"project_root": self.project_root, "sessions": self.sessions.list()}
        if method == "GET" and path == "/events":
            with self.lock:
                return 200, {"events": list(self.events)}
        if method == "GET" and path == "/sessions":
            return 200, {"sessions": self.sessions.list()}
        if method == "GET" and path == "/gpu":
            return 200, local_server.gpu_status()
        if method == "GET" and path == "/workspace":
            return 200, workspace_status(self.project_root)
        if method == "GET" and path == "/local-server/status":
            return 200, local_server.status()
        if method == "GET" and path == "/local-server/models":
            models = local_server.list_available_models()
            return 200, {"models": [vars(m) for m in models]}
        if method == "POST" and path == "/local-server/start":
            body = body or {}
            try:
                result = local_server.start_big_model(
                    quant=body.get("quant", "q2_k"),
                    n_ctx=int(body.get("n_ctx", 65536)),
                    slot_cache=bool(body.get("slot_cache", True)),
                )
                if not body.get("no_draft", False):
                    local_server.start_draft_model()
            except (ValueError, FileNotFoundError) as exc:
                return 400, {"error": str(exc)}
            return 200, result
        if method == "POST" and path == "/local-server/switch":
            body = body or {}
            quant = body.get("quant")
            if not quant:
                return 400, {"error": "quant is required"}
            try:
                result = local_server.switch_big_model(
                    quant=quant,
                    n_ctx=int(body.get("n_ctx", 65536)),
                    slot_cache=bool(body.get("slot_cache", True)),
                )
            except ValueError as exc:
                return 400, {"error": str(exc)}
            return 200, result
        if method == "POST" and path == "/local-server/stop":
            body = body or {}
            result = {}
            if body.get("big", True):
                result["big"] = local_server.stop("big")
            if body.get("draft", True):
                result["draft"] = local_server.stop("draft")
            return 200, result
        if method == "POST" and path in {"/plan", "/run", "/review"}:
            body = body or {}
            request = body.get("request", "Review current uncommitted changes" if path == "/review" else "")
            if path != "/review" and not request:
                return 400, {"error": "request is required"}
            session_id = body.get("session_id") or f"session-{uuid4().hex[:12]}"
            coordinator = self._coordinator()
            if path == "/plan":
                result = asyncio.run(coordinator.plan_only(request))
            elif path == "/review":
                result = asyncio.run(coordinator.review_changes())
            else:
                result = asyncio.run(coordinator.run(request))
            record = self.sessions.save(session_id, request=request, phase=path[1:], result=result)
            return 200, record
        return 404, {"error": "not found"}

    def serve(self, host: str = "127.0.0.1", port: int = 8787) -> None:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        if not is_loopback and not self.token:
            raise ValueError("A remote control token is required for non-loopback binds")
        control = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _respond_html(self, status: int, html: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

            def _respond_asset(self, status: int, asset: Path, content_type: str) -> None:
                try:
                    data = asset.read_bytes()
                except OSError:
                    self._respond(404, {"error": "ui asset not found"})
                    return
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path in ("/", "/index.html"):
                    # Static markup only, no data -- served without the
                    # token gate so the page itself always loads; every
                    # fetch() it makes still goes through the JSON API
                    # below, which is authorized normally.
                    try:
                        self._respond_html(200, _WEBUI_INDEX.read_bytes())
                    except OSError:
                        self._respond(404, {"error": "ui assets not found"})
                    return
                if self.path in _WEBUI_ASSETS:
                    asset, content_type = _WEBUI_ASSETS[self.path]
                    self._respond_asset(200, asset, content_type)
                    return
                if not self._authorized():
                    self._respond(401, {"error": "authentication required"})
                    return
                status, payload = control.handle("GET", self.path)
                self._respond(status, payload)

            def do_POST(self) -> None:
                if not self._authorized():
                    self._respond(401, {"error": "authentication required"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._respond(400, {"error": "invalid JSON"})
                    return
                status, payload = control.handle("POST", self.path, body)
                self._respond(status, payload)

            def _authorized(self) -> bool:
                if control.token is None:
                    return True
                presented = self.headers.get("Authorization", "")
                if presented.startswith("Bearer "):
                    presented = presented[7:]
                return hmac.compare_digest(presented, control.token)

            def log_message(self, *_args: Any) -> None:
                return

        ThreadingHTTPServer((host, port), Handler).serve_forever()
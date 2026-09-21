"""Local HTTP control plane for remote terminal sessions."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any
from uuid import uuid4

from local_coder.orchestrator.config_loader import load_config
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.orchestrator.sessions import SessionStore
from local_coder.types import AgentEvent, AgentRole


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
        self.lock = Lock()
        self.sessions = SessionStore(project_root)

    def _event(self, event: AgentEvent) -> None:
        with self.lock:
            self.events.append(event.model_dump(mode="json"))

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

            def do_GET(self) -> None:
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
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
import os
import subprocess
import mimetypes
from urllib.parse import urlsplit, unquote
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from local_coder.workbench import WorkbenchAPI
from local_coder.file_lock import exclusive_lock
from local_coder.workspace import Workspace
from local_coder import local_server
from local_coder.orchestrator.config_loader import load_config
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.orchestrator.sessions import SessionError, SessionStore
from local_coder.types import AgentEvent, AgentRole, AgentMode, ModelConfig

_WEBUI_ROOT = Path(__file__).parent / "webui" / "dist"
_WEBUI_INDEX = _WEBUI_ROOT / "index.html"
_WEBUI_ASSETS = {
    "/manifest.webmanifest": (Path(__file__).parent / "webui" / "public" / "manifest.webmanifest", "application/manifest+json"),
    "/sw.js": (Path(__file__).parent / "webui" / "public" / "sw.js", "application/javascript"),
    "/icon.svg": (Path(__file__).parent / "webui" / "public" / "icon.svg", "image/svg+xml"),
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
        line for line in _git(project_root, "ls-files", "--others", "--exclude-standard").splitlines() if line and not line.startswith(".local-coder/")
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
        "status": _git(project_root, "status", "-s", "--", ".", ":(exclude).local-coder").strip(),
    }


class RemoteControlServer(WorkbenchAPI):
    """Serve read-only status plus explicit plan/run/review actions."""

    def __init__(
        self,
        project_root: str,
        config_path: str | None = None,
        model_name: str | None = None,
        yolo: bool = False,
        token: str | None = None,
        config_overrides: list[str] | None = None,
        temperature: float | None = None,
    ):
        self.project_root = project_root
        self.config_path = config_path
        self.model_name = model_name
        # --set / --temperature from the command line that started the server.
        self.config_overrides = list(config_overrides or [])
        self.temperature = temperature
        # The authenticated workbench supplies a human approval inbox.
        # Unanswered requests expire as denials; model tools cannot respond.
        self.yolo = yolo
        self.token = token
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self._event_seq = 0
        self.lock = Lock()
        from local_coder.statedir import state_path
        state_path(project_root, '.local-coder/bootstrap')
        self.sessions = SessionStore(project_root)
        self._init_workbench()
        self._lease_file = None
        self._lease = None
        self._connection_file = None

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
            payload["session_id"] = getattr(self._context, "session_id", None)
            job = self._jobs.get(payload["session_id"])
            if job is not None and (event.event_type in {"task_failed", "model_error", "plan_error"} or
                                    (event.event_type == "task_status" and event.data.get("status") == "failed")):
                job["failed"] = True
            payload = self._redact(payload)
            if job is not None:
                job.setdefault("events", []).append(payload)
                job["events"] = job["events"][-600:]
                if event.event_type == "checkpoint_saved":
                    job["checkpoint"] = event.data.get("path")
            self.events.append(payload)
        if job is not None and event.event_type == "checkpoint_saved":
            # Make Resume available even when the process dies before the job finalizer.
            self.sessions.save(payload["session_id"], checkpoint=job["checkpoint"], events=list(job["events"]))

    def _coordinator(self) -> Coordinator:
        config = load_config(
            self.config_path, project_root=self.project_root,
            overrides=self.config_overrides, temperature=self.temperature,
        )
        provider = self.local_inference_provider() if self._using_inference else self._provider
        if self._using_inference and provider is None:
            raise ValueError("The on-device model is not ready")
        if provider is not None:
            config.models = {"workbench": provider.model_copy(deep=True)}
            config.agentic.role_models = {role.value: "workbench" for role in AgentRole}
        elif self.model_name:
            config.agentic.role_models = {role.value: self.model_name for role in AgentRole}
        if self.yolo:
            config.approval.require_approval_for_commands = False
            config.approval.require_approval_for_commits = False
        config.harness = self.harness_settings()
        mode = getattr(self._context, "mode", config.mode)
        config.mode = mode
        if mode == AgentMode.BUG_HUNTING:
            from local_coder.statedir import state_path
            mapping_path = state_path(self.project_root, ".local-coder/bug-models.json")
            mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
            roles = {"lead": ["orchestrator", "planner"],
                     "delegate": ["explorer", "coder", "debugger", "tester", "reviewer"],
                     "exploit": ["bug_hunter", "security", "exploit_validator"]}
            for purpose, profile_id in mapping.items():
                if profile_id:
                    record = self.provider_profiles.get(profile_id)
                    model = ModelConfig.model_validate({**record["provider"], "name": "bug-" + purpose})
                    config.models[model.name] = model
                    for role in roles[purpose]:
                        config.agentic.role_models[role] = model.name
        from local_coder.providers.context_policy import context_policy
        selected = provider or config.models.get(self.model_name or "default") or next(iter(config.models.values()), None)
        if selected is not None and selected.auto_context:
            policy = context_policy(selected, mode, config.harness)
            config.agentic.context_window_chars = policy["context_window_chars"]
            config.agentic.compact_context_chars = policy["compact_context_chars"]
            config.harness.memory_context_chars = policy["memory_chars"]
            for model in config.models.values():
                if model.auto_context:
                    model.max_tokens = context_policy(model, mode, config.harness)["completion_tokens"]
        safety_tier = self.harness_safety_tier()
        if safety_tier is not None:
            config.safety_tier = safety_tier
        coordinator = Coordinator(config=config, project_root=self.project_root, approval_callback=self.approvals.request,
                                  quota_guard=self.free_quota_guard)
        if hasattr(coordinator, "tool_registry"):
            coordinator.tool_registry.goal_id = getattr(self._context, "goal_id", None)
        self.current_budget = getattr(coordinator, "budget", None)
        self.current_registry = getattr(coordinator, "tool_registry", None)
        coordinator.on_event(self._event)
        return coordinator

    def handle(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        if body is not None and not isinstance(body, dict):
            return 400, {"error": "JSON body must be an object"}
        workbench = self.workbench_handle(method, path, body)
        if workbench is not None:
            return workbench
        if method == "GET" and path == "/identity":
            return 200, {"project_root": self.project_root, "pid": os.getpid()}
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
            if not self._job_lock.acquire(blocking=False):
                return 409, {"error": "A task is already running in this workspace"}
            try:
                if path == "/run":
                    return self._run_in_session(session_id, request)
                coordinator = self._coordinator()
                async def execute():
                    try:
                        if path == "/plan":
                            return await coordinator.plan_only(request)
                        return await coordinator.review_changes()
                    finally:
                        await coordinator.model_manager.close_all()
                result = asyncio.run(execute())
            finally:
                self._job_lock.release()
            record = self.sessions.save(session_id, request=request, phase=path[1:], result=result)
            return 200, record
        return 404, {"error": "not found"}

    def _run_in_session(self, session_id: str, request: str) -> tuple[int, dict[str, Any]]:
        """/run in a persistent session, the same kind the CLI uses: a
        repeated session_id is a follow-up that sees the earlier turns, and
        a run that fails can be picked up with `local-coder resume <id>`."""
        try:
            session = self.sessions.open_or_create(session_id)
            session.acquire()
        except SessionError as exc:
            return (409 if "already running" in str(exc) else 400), {"error": str(exc)}
        try:
            turn = session.start_turn(request)
            coordinator = self._coordinator()
            coordinator.on_event(session.record_event)
            async def execute():
                try:
                    return await coordinator.run(
                        request, checkpoint=session.checkpoint(turn), history=turn["context"],
                    )
                finally:
                    manager = getattr(coordinator, "model_manager", None)
                    if manager is not None:
                        await manager.close_all()
            try:
                result = asyncio.run(execute())
            except Exception as exc:
                session.finish_turn(turn, "failed", error=self._redact(str(exc)))
                return 500, {"error": self._redact(str(exc)), "session_id": session_id}
            session.finish_turn(turn, "completed", report=result)
            return 200, self.sessions.get(session_id)
        finally:
            session.release()

    def make_http_server(self, host: str = "127.0.0.1", port: int = 8787):
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        if not is_loopback and not self.token:
            raise ValueError("A remote control token is required for non-loopback binds")
        control = self

        class Handler(BaseHTTPRequestHandler):
            def end_headers(self):
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                super().end_headers()

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
                path = urlsplit(self.path).path
                if path in {"/", "/index.html"}:
                    try:
                        self._respond_html(200, _WEBUI_INDEX.read_bytes())
                    except OSError:
                        self._respond(503, {"error": "Build the web UI before starting the server: npm run build in src/local_coder/webui"})
                    return
                if path in _WEBUI_ASSETS:
                    asset, content_type = _WEBUI_ASSETS[path]
                    self._respond_asset(200, asset, content_type)
                    return
                if path.startswith("/assets/") or path in {"/favicon.svg", "/icons.svg"}:
                    asset = (_WEBUI_ROOT / unquote(path).lstrip("/")).resolve()
                    if not asset.is_relative_to(_WEBUI_ROOT.resolve()) or not asset.is_file():
                        self._respond(404, {"error": "Asset not found"})
                        return
                    self._respond_asset(200, asset, mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
                    return
                if not self._authorized():
                    self._respond(401, {"error": "authentication required"})
                    return
                try:
                    status, payload = control.handle("GET", self.path)
                    self._respond(status, payload)
                except Exception:
                    self._respond(500, {"error": "Request failed; check the server configuration"})

            def do_POST(self) -> None:
                if not self._authorized():
                    self._respond(401, {"error": "authentication required"})
                    return
                origin = self.headers.get("Origin")
                if origin and origin not in {f"http://{self.headers.get('Host')}", f"https://{self.headers.get('Host')}"}:
                    self._respond(403, {"error": "Cross-origin requests are not allowed"})
                    return
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self._respond(415, {"error": "Use application/json"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 100000:
                        self._respond(413, {"error": "Request body exceeds 100 KB"})
                        return
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, UnicodeError):
                    self._respond(400, {"error": "invalid JSON"})
                    return
                try:
                    status, payload = control.handle("POST", self.path, body)
                    self._respond(status, payload)
                    if self.path == "/shutdown" and status == 200:
                        from threading import Thread
                        Thread(target=self.server.shutdown, daemon=True).start()
                except Exception:
                    self._respond(500, {"error": "Request failed; check the server configuration"})

            def _authorized(self) -> bool:
                if is_loopback and self.headers.get("Host") not in {
                    f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}",
                    f"[::1]:{self.server.server_port}",
                }:
                    return False
                if control.token is None:
                    return True
                presented = self.headers.get("Authorization", "")
                if presented.startswith("Bearer "):
                    presented = presented[7:]
                return hmac.compare_digest(presented.encode("utf-8"), control.token.encode("utf-8"))

            def log_message(self, *_args: Any) -> None:
                return

        class WorkspaceHTTPServer(ThreadingHTTPServer):
            def server_close(server):
                super().server_close()
                control.release_workspace()
        if self._lease is not None:
            raise ValueError("Workspace already has an engine on this server")
        lease_path = Workspace(self.project_root).resolve(".local-coder/engine.lock")
        self._lease_file = lease_path.open("a+")
        self._lease = exclusive_lock(self._lease_file, blocking=False)
        try:
            self._lease.__enter__()
        except OSError:
            self._lease_file.close()
            self._lease_file = self._lease = None
            raise ValueError("Workspace already has an engine. Share its connection or close the other app first") from None
        try:
            server = WorkspaceHTTPServer((host, port), Handler)
            self.reconcile_sessions()
            from local_coder.engine_discovery import publish_connection
            self._connection_file = publish_connection(self.project_root, f"http://127.0.0.1:{server.server_port}", self.token) if host == "127.0.0.1" else None
            return server
        except BaseException:
            if "server" in locals():
                server.server_close()
            self.release_workspace()
            raise

    def release_workspace(self):
        if self._connection_file is not None:
            self._connection_file.unlink(missing_ok=True)
            self._connection_file = None
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None
        if self._lease_file is not None:
            self._lease_file.close()
            self._lease_file = None

    def serve(self, host: str = "127.0.0.1", port: int = 8787) -> None:
        server = self.make_http_server(host, port)
        try:
            server.serve_forever()
        finally:
            self.drain_jobs()
            server.server_close()

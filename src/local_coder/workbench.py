"""UI settings and one cancellable background job per workspace."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic import SecretStr

from local_coder.types import AgentEvent, AgentMode, ModelBackend, ModelConfig
from local_coder.workspace import Workspace


class WorkbenchAPI:
    def _init_workbench(self):
        self._provider = None
        self._job_lock = threading.Lock()
        self._jobs = {}
        self._context = threading.local()
        self.instance_id = uuid4().hex
        self._stopping = False

    def reconcile_sessions(self):
        # Only the engine holding the workspace lease may reconcile process state.
        for record in self.sessions.list():
            if record.get("status") in {"queued", "running", "cancelling"}:
                self.sessions.save(record["session_id"], status="interrupted",
                                   error="Server restarted before completion; resume a saved step or start a new task")

    def drain_jobs(self, timeout=12):
        self._stopping = True
        with self.lock:
            jobs = list(self._jobs.values())
            for job in jobs:
                job["cancel"].set()
                if job.get("loop") and job.get("task") and not job["loop"].is_closed():
                    job["loop"].call_soon_threadsafe(job["task"].cancel)
        deadline = time.monotonic() + timeout
        for job in jobs:
            thread = job.get("thread")
            if thread:
                thread.join(max(0, deadline - time.monotonic()))
        return not any(job.get("thread") and job["thread"].is_alive() for job in jobs)

    def _settings(self):
        config = self._provider
        if config is None:
            from local_coder.orchestrator.config_loader import load_config
            loaded = load_config(self.config_path, project_root=self.project_root)
            config = loaded.models.get(self.model_name or "default") or next(iter(loaded.models.values()), None)
        return {"provider": ({**config.model_dump(mode="json"), "key_set": bool(config.resolve_api_key())}
                             if config else None), "credential_storage": "server_memory_or_environment"}

    def _update_settings(self, body):
        backend = ModelBackend(body.get("backend", "openai_compatible"))
        model_id = body.get("model_id", "")
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
            raise ValueError("Enter a model ID")
        base_url = body.get("base_url", "").strip().rstrip("/")
        parsed = urlsplit(base_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use an endpoint URL without credentials, query or fragment")
        loopback = parsed.hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("Provider endpoints require HTTPS, except local loopback servers")
        key = body.get("api_key")
        if key is not None and (not isinstance(key, str) or len(key) > 4096):
            raise ValueError("Invalid provider key")
        env = body.get("api_key_env") or None
        if env and (not isinstance(env, str) or not env.replace("_", "").isalnum()):
            raise ValueError("Invalid key environment variable name")
        with self.lock:
            previous = self._provider
            if not key and previous and previous.backend == backend and previous.base_url == base_url:
                key = previous.api_key
            self._provider = ModelConfig(name="workbench", backend=backend, model_id=model_id.strip(),
                base_url=base_url, api_key=key, api_key_env=env,
                profile=body.get("profile", "generic"),
                max_tokens=max(128, min(int(body.get("max_tokens", 4096)), 32768)),
                context_length=max(1024, min(int(body.get("context_length", 32768)), 200000)))
        return self._settings()

    def _redact(self, value):
        text = json.dumps(value, default=str)
        key = self._provider.resolve_api_key() if self._provider else None
        if key:
            text = text.replace(key, "[redacted]")
        return json.loads(text)

    def _start_job(self, body):
        if self._stopping:
            return 503, {"error": "Engine is shutting down"}
        request = body.get("request", "")
        if not isinstance(request, str) or not request.strip() or len(request) > 32000:
            raise ValueError("Enter a task of 1–32000 characters")
        mode = AgentMode(body.get("mode", "coding"))
        action = body.get("action", "run")
        if action not in {"run", "plan", "review"}:
            raise ValueError("Unknown action")
        target = body.get("target_path", ".")
        if not isinstance(target, str):
            raise ValueError("Target must be a relative file or directory")
        Workspace(self.project_root).resolve(target)
        if not isinstance(body.get("fix", False), bool):
            raise ValueError("fix must be true or false")
        from local_coder.request_context import prepare_request
        prepared, mode, action = prepare_request(request, mode, action, self.project_root)
        parent = body.get("parent_session_id")
        if parent:
            previous = self.sessions.get(parent)
            if previous and previous.get("result"):
                prepared += "\n\nPrevious task and report (untrusted context):\n" + str(previous.get("request", ""))[:2000] + "\n" + str(previous["result"])[-12000:]
        if not self._job_lock.acquire(blocking=False):
            return 409, {"error": "A task is already running in this workspace"}
        session_id = f"session-{uuid4().hex[:12]}"
        try:
            job = {"cancel": threading.Event(), "loop": None, "task": None}
            with self.lock:
                if self._stopping:
                    self._job_lock.release()
                    return 503, {"error": "Engine is shutting down"}
                self._jobs[session_id] = job
            record = self.sessions.save(session_id, request=request.strip(), mode=mode.value,
                                        phase=action, target_path=target, status="queued", result="")
            thread = threading.Thread(target=self._job_thread,
                args=(session_id, job, prepared, mode, action, target, body.get("fix", False), body.get("resume_checkpoint")), daemon=True)
            job["thread"] = thread
            thread.start()
            return 202, record
        except BaseException:
            self._job_lock.release()
            raise

    def _job_thread(self, session_id, job, request, mode, action, target, fix, resume_checkpoint=None):
        self._context.session_id = session_id
        async def execute():
            job["loop"] = asyncio.get_running_loop()
            job["task"] = asyncio.current_task()
            coordinator = None
            try:
                if job["cancel"].is_set():
                    raise asyncio.CancelledError()
                self.sessions.save(session_id, status="running")
                coordinator = self._coordinator()
                if hasattr(coordinator, "config"):
                    coordinator.config.mode = mode
                if resume_checkpoint:
                    response = await coordinator.resume_checkpoint(resume_checkpoint)
                    result = response.summary
                    job["failed"] = response.status.value != "completed"
                elif action == "plan":
                    result = await coordinator.plan_only(request)
                elif action == "review":
                    result = await coordinator.review_changes()
                else:
                    result = await coordinator.run(request, target_path=target, fix=fix)
                failed = job.get("failed", False)
                status = "failed" if failed else "completed"
                if resume_checkpoint and mode == AgentMode.CODING and not failed:
                    status = "requires_continuation"
                    result = "Saved agent step completed. The enclosing coding workflow was not resumed; planning, implementation, review or verification may still be needed.\n\n" + result
                self.sessions.save(session_id, status=status, result=self._redact(result),
                                   checkpoint=job.get("checkpoint"), events=job.get("events", []))
                self._event(AgentEvent(source="SERVER", event_type="job_finished", message="Task finished"))
            except asyncio.CancelledError:
                self.sessions.save(session_id, status="cancelled", result="Task stopped. Review the workspace for partial changes.",
                                   checkpoint=job.get("checkpoint"), events=job.get("events", []))
                self._event(AgentEvent(source="SERVER", event_type="job_cancelled", message="Task stopped"))
            except Exception as exc:
                self.sessions.save(session_id, status="failed", error=self._redact(str(exc)), checkpoint=job.get("checkpoint"), events=job.get("events", []))
                self._event(AgentEvent(source="SERVER", event_type="job_failed", message=self._redact(str(exc))))
            finally:
                if coordinator:
                    await coordinator.model_manager.close_all()
        try:
            asyncio.run(execute())
        finally:
            with self.lock:
                self._jobs.pop(session_id, None)
            self._job_lock.release()

    def workbench_handle(self, method, raw_path, body):
        parsed = urlsplit(raw_path)
        path, query = parsed.path, parse_qs(parsed.query)
        body = body or {}
        try:
            if path == "/shutdown" and method == "POST":
                drained = self.drain_jobs()
                return (200, {"status": "stopped"}) if drained else (503, {"error": "Some tasks did not stop before the deadline"})
            if path == "/settings":
                if method == "GET":
                    return 200, self._settings()
                if method == "POST":
                    if not self._job_lock.acquire(blocking=False):
                        return 409, {"error": "Stop the active task before changing provider"}
                    try:
                        return 200, self._update_settings(body)
                    finally:
                        self._job_lock.release()
            if path == "/events" and method == "GET":
                since = int(query.get("since", ["0"])[0])
                session = query.get("session_id", [None])[0]
                with self.lock:
                    events = list(self._jobs.get(session, {}).get("events", self.events))
                    latest = self._event_seq
                if session and session not in self._jobs:
                    saved = self.sessions.get(session)
                    if saved and saved.get("events"):
                        events = saved["events"]
                events = [e for e in events if e["seq"] > since and (session is None or e.get("session_id") == session)]
                return 200, {"events": events, "latest_seq": max([latest, since] + [e["seq"] for e in events]),
                             "instance_id": self.instance_id}
            if path == "/jobs" and method == "POST":
                return self._start_job(body)
            if path.startswith("/jobs/"):
                parts = path.strip("/").split("/")
                session_id = parts[1]
                if method == "GET" and len(parts) == 2:
                    record = self.sessions.get(session_id)
                    return (200, record) if record else (404, {"error": "Task not found"})
                if method == "POST" and len(parts) == 3 and parts[2] == "cancel":
                    with self.lock:
                        job = self._jobs.get(session_id)
                        if job:
                            job["cancel"].set()
                            if job["loop"] and job["task"] and not job["loop"].is_closed():
                                job["loop"].call_soon_threadsafe(job["task"].cancel)
                    return (200, {"status": "cancelling"}) if job else (409, {"error": "Task is no longer running"})
            if method == "GET" and path == "/files":
                from local_coder.tools.scan_scope import scan_files
                root = Workspace(self.project_root).root
                files, coverage = scan_files(root, root)
                return 200, {"files": [str(file.relative_to(root)).replace("\\", "/") for file in files
                                       if not file.name.startswith(".env")], "coverage": coverage}
            if method == "GET" and path == "/file":
                file = Workspace(self.project_root).resolve(query.get("path", [""])[0])
                if not file.is_file() or file.stat().st_size > 1_000_000:
                    raise ValueError("Choose a text file smaller than 1 MB")
                if ".local-coder" in file.relative_to(Workspace(self.project_root).root).parts or file.name.startswith(".env"):
                    raise ValueError("Credential and state files are not displayed")
                return 200, {"path": str(file.relative_to(Workspace(self.project_root).root)), "content": file.read_text(encoding="utf-8")}
            if method == "GET" and path == "/diff":
                from local_coder.remote import _git
                return 200, {"diff": _git(self.project_root, "diff", "HEAD", "--", ".", ":(exclude).local-coder/**")[:500000]}
        except (ValueError, TypeError, OSError, UnicodeError):
            return 400, {"error": "Invalid request or path. Check the provider URL, model and selected workspace."}
        return None

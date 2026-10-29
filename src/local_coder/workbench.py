"""UI settings and one cancellable background job per workspace."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import ipaddress
import json
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic import SecretStr

from local_coder.types import AgentEvent, AgentMode, ModelBackend, ModelConfig
from local_coder.workspace import Workspace


def workspace_fingerprint(project_root: str) -> str:
    """Fingerprint workspace edits so undo can refuse to discard newer work."""
    workspace = Workspace(project_root)
    root = workspace.root
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, timeout=10,
        ).stdout
        staged = subprocess.run(
            ["git", "diff", "--cached", "--binary", "HEAD", "--", ".", ":(exclude).local-coder/**"],
            cwd=root, capture_output=True, check=True, timeout=10,
        ).stdout
        tracked = subprocess.run(
            ["git", "diff", "--binary", "--", ".", ":(exclude).local-coder/**"],
            cwd=root, capture_output=True, check=True, timeout=10,
        ).stdout
        listing = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, check=True, timeout=10,
        ).stdout.decode("utf-8", errors="strict")
        digest = hashlib.sha256(head + staged + tracked)
        for relative in sorted(path for path in listing.splitlines() if path and not path.startswith(".local-coder/")):
            file = workspace.resolve(relative)
            if file.is_file():
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(file.read_bytes())
                digest.update(b"\0")
        return digest.hexdigest()
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Unable to verify workspace state before undo: {exc}") from exc


class WorkbenchAPI:
    def _init_workbench(self):
        from local_coder.redteam import EngagementStore
        self.engagements = EngagementStore(self.project_root)
        from local_coder.context.code_index import CodeIndex
        self.code_index = CodeIndex(self.project_root)
        from local_coder.inference import EmbeddedInferenceManager
        self.inference = EmbeddedInferenceManager(Workspace(self.project_root).resolve(".local-coder/inference"))
        self._using_inference = False
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
        self.inference.stop()
        return not any(job.get("thread") and job["thread"].is_alive() for job in jobs)

    def local_inference_provider(self):
        endpoint = self.inference.endpoint_config()
        if not endpoint:
            return None
        return ModelConfig(name="on-device", backend=ModelBackend.OPENAI_COMPAT,
            model_id=endpoint["model"], base_url=endpoint["base_url"], api_key=endpoint["api_key"],
            context_length=self.inference.status()["settings"]["context_size"], max_tokens=min(4096, self.inference.status()["settings"]["context_size"] // 2))

    def _settings(self):
        config = self.local_inference_provider() if self._using_inference else self._provider
        if self._using_inference and config is None:
            return {"provider": None, "credential_storage": "ephemeral_local_runtime", "loading_local_model": True}
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
                strategy_attempt_limit=int(body.get("strategy_attempt_limit", 3)),
                max_tokens=max(128, min(int(body.get("max_tokens", 4096)), 32768)),
                context_length=max(1024, min(int(body.get("context_length", 32768)), 2000000)))
        self._using_inference = False
        return self._settings()

    def _redact(self, value):
        text = json.dumps(value, default=str)
        selected_provider = self.local_inference_provider() if self._using_inference else self._provider
        key = selected_provider.resolve_api_key() if selected_provider else None
        if key:
            text = text.replace(key, "[redacted]")
        return json.loads(text)

    def _start_job(self, body):
        if self._stopping:
            return 503, {"error": "Engine is shutting down"}
        if self._using_inference and not self.inference.endpoint_config():
            return 409, {"error": "The on-device model is not ready. Check its status in Model settings."}
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
        engagement_id = body.get("engagement_id")
        if mode == AgentMode.RED_TEAM:
            if not isinstance(engagement_id, str) or action != "run" or body.get("fix", False):
                raise ValueError("Red team tasks require a saved engagement and the Run action; fixes are a separate task")
            self.engagements.load(engagement_id)
            if body.get("resume_checkpoint"):
                checkpoint = Workspace(self.project_root).resolve(body["resume_checkpoint"])
                if not checkpoint.is_file() or checkpoint.stat().st_size > 5_000_000:
                    raise ValueError("Invalid checkpoint")
                # Coordinator still performs full state-directory and run identity validation.
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                task_data = saved.get("task") if isinstance(saved, dict) else None
                if not isinstance(task_data, dict) or task_data.get("engagement_id") != engagement_id:
                    raise ValueError("Checkpoint belongs to another engagement")
        parent = body.get("parent_session_id")
        if parent:
            previous = self.sessions.get(parent)
            if mode == AgentMode.RED_TEAM and (not previous or previous.get("engagement_id") != engagement_id):
                raise ValueError("Follow-up context must belong to this engagement")
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
                                        phase=action, target_path=target, status="queued", result="",
                                        engagement_id=engagement_id, started_at=time.time())
            thread = threading.Thread(target=self._job_thread,
                args=(session_id, job, prepared, mode, action, target, body.get("fix", False), body.get("resume_checkpoint"), engagement_id), daemon=True)
            job["thread"] = thread
            thread.start()
            return 202, record
        except BaseException:
            self._job_lock.release()
            raise

    def _job_thread(self, session_id, job, request, mode, action, target, fix, resume_checkpoint=None, engagement_id=None):
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
                    options = {"target_path": target, "fix": fix}
                    if mode == AgentMode.RED_TEAM:
                        options["engagement_id"] = engagement_id
                    result = await coordinator.run(request, **options)
                failed = job.get("failed", False)
                status = "failed" if failed else "completed"
                rollback_error = None
                if failed:
                    rollback_error = self._auto_rollback_failed_run(job, coordinator, mode)
                    if job.get("rollback_applied"):
                        result += "\n\nWorkspace restored to the pre-run checkpoint because verification failed."
                    elif rollback_error:
                        result += f"\n\nAutomatic rollback was not applied: {rollback_error}"
                if resume_checkpoint and mode == AgentMode.CODING and not failed:
                    status = "requires_continuation"
                    result = "Saved agent step completed. The enclosing coding workflow was not resumed; planning, implementation, review or verification may still be needed.\n\n" + result
                self.sessions.save(session_id, status=status, result=self._redact(result),
                                   rollback_applied=job.get("rollback_applied", False),
                                   checkpoint=job.get("checkpoint"),
                                   pre_run_checkpoint_id=job.get("pre_run_checkpoint_id"),
                                   workspace_fingerprint=self._workspace_fingerprint(),
                                   events=job.get("events", []))
                self._event(AgentEvent(source="SERVER", event_type="job_finished", message="Task finished"))
            except asyncio.CancelledError:
                self.sessions.save(session_id, status="cancelled", result="Task stopped. Review the workspace for partial changes.",
                                   checkpoint=job.get("checkpoint"),
                                   pre_run_checkpoint_id=job.get("pre_run_checkpoint_id"),
                                   workspace_fingerprint=self._workspace_fingerprint(),
                                   events=job.get("events", []))
                self._event(AgentEvent(source="SERVER", event_type="job_cancelled", message="Task stopped"))
            except Exception as exc:
                rollback_error = self._auto_rollback_failed_run(job, coordinator, mode) if coordinator else None
                error = str(exc)
                if job.get("rollback_applied"):
                    error += "\nWorkspace restored to the pre-run checkpoint because the task failed."
                elif rollback_error:
                    error += f"\nAutomatic rollback was not applied: {rollback_error}"
                self.sessions.save(session_id, status="failed", error=self._redact(error),
                                   rollback_applied=job.get("rollback_applied", False),
                                   checkpoint=job.get("checkpoint"),
                                   pre_run_checkpoint_id=job.get("pre_run_checkpoint_id"),
                                   workspace_fingerprint=self._workspace_fingerprint(),
                                   events=job.get("events", []))
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

    def _auto_rollback_failed_run(self, job, coordinator, mode):
        verification = getattr(getattr(coordinator, "config", None), "verification", None)
        if mode != AgentMode.CODING or not getattr(verification, "rollback_on_failure", False):
            return None
        checkpoint_id = job.get("pre_run_checkpoint_id")
        if not checkpoint_id:
            return "the task has no pre-run workspace checkpoint"
        try:
            from local_coder.git.checkpoints import CheckpointManager
            manager = CheckpointManager(self.project_root)
            checkpoint = manager.get(checkpoint_id)
            checkpoints = manager.list()
            if not checkpoints or checkpoints[0].checkpoint_id != checkpoint_id:
                return "a newer checkpoint exists"
            current_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.project_root,
                capture_output=True, check=True, timeout=10,
            ).stdout.decode().strip()
            if current_head != checkpoint.head:
                return "the Git HEAD changed during the task"
            manager.rollback(checkpoint_id)
            job["rollback_applied"] = True
            return None
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            return str(exc)

    def _workspace_fingerprint(self):
        try:
            return workspace_fingerprint(self.project_root)
        except RuntimeError as exc:
            self._event(AgentEvent(source="SERVER", event_type="undo_unavailable", message=str(exc)))
            return None

    def workbench_handle(self, method, raw_path, body):
        parsed = urlsplit(raw_path)
        path, query = parsed.path, parse_qs(parsed.query)
        body = body or {}
        try:
            if path == "/undo" and method == "POST":
                session_id = body.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    return 400, {"error": "Select the task run to undo"}
                if not self._job_lock.acquire(blocking=False):
                    return 409, {"error": "Stop the active task before undoing workspace changes"}
                try:
                    from local_coder.git.checkpoints import CheckpointManager
                    record = self.sessions.get(session_id)
                    if not record or record.get("status") not in {"completed", "cancelled", "failed"}:
                        return 409, {"error": "This task does not have a finished workspace snapshot to undo"}
                    checkpoint_id = record.get("pre_run_checkpoint_id")
                    expected_fingerprint = record.get("workspace_fingerprint")
                    if not checkpoint_id or not expected_fingerprint:
                        return 409, {"error": "This task has no verified pre-run checkpoint"}
                    if workspace_fingerprint(self.project_root) != expected_fingerprint:
                        return 409, {"error": "Workspace changed after this task finished. Review the current diff; undo was not applied."}
                    manager = CheckpointManager(self.project_root)
                    checkpoints = manager.list()
                    if not checkpoints or checkpoints[0].checkpoint_id != checkpoint_id:
                        return 409, {"error": "A newer checkpoint exists. Undo it from its own task instead."}
                    checkpoint = manager.rollback(checkpoint_id)
                    self.sessions.save(session_id, undo_applied=True)
                    return 200, {"checkpoint_id": checkpoint.checkpoint_id}
                except (ValueError, RuntimeError) as exc:
                    return 409, {"error": str(exc)}
                finally:
                    self._job_lock.release()
            if path == "/security-playbooks" and method == "GET":
                from local_coder.security_playbooks import PLAYBOOKS
                return 200, {"playbooks": list(PLAYBOOKS.values())}
            if path == "/inference" and method == "GET":
                return 200, {**self.inference.status(), "selected_provider": self._using_inference}
            if path.startswith("/inference/") and method == "POST":
                if not self._job_lock.acquire(blocking=False):
                    return 409, {"error": "Stop the active task before changing the on-device runtime"}
                try:
                    if path == "/inference/configure":
                        return 200, self.inference.configure(body)
                    if path == "/inference/start":
                        result = self.inference.start(body or None)
                        self._using_inference = True
                        return 202, result
                    if path == "/inference/stop":
                        result = self.inference.stop()
                        self._using_inference = False
                        return 200, result
                    if path == "/inference/install-runtime":
                        return 202, self.inference.install_runtime(body.get("asset_name"))
                finally:
                    self._job_lock.release()
            if path == "/engagements":
                if method == "GET":
                    return 200, {"engagements": self.engagements.list()}
                if method == "POST":
                    from local_coder.redteam import EngagementScope
                    return 201, self.engagements.create(EngagementScope.model_validate(body))
            if path.startswith("/engagements/") and method == "GET":
                parts = path.strip("/").split("/")
                if len(parts) == 2:
                    return 200, self.engagements.load(parts[1])
                if len(parts) == 3 and parts[2] == "report":
                    return 200, self.engagements.report(parts[1])
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
                target = query.get("path", [None])[0]
                if target:
                    workspace = Workspace(self.project_root)
                    relative = workspace.relative_path(target)
                    file = workspace.resolve(relative)
                    if ".local-coder" in file.relative_to(workspace.root).parts or file.name.startswith(".env"):
                        raise ValueError("Credential and state files are not displayed")
                    diff = _git(self.project_root, "diff", "HEAD", "--", relative)
                    is_untracked = _git(
                        self.project_root, "status", "--porcelain", "--untracked-files=normal", "--", relative
                    ).startswith("??")
                    if not diff and is_untracked and file.is_file():
                        if file.stat().st_size > 500_000:
                            raise ValueError("Choose a file smaller than 500 KB")
                        try:
                            content = file.read_text(encoding="utf-8").splitlines(keepends=True)
                        except UnicodeError as exc:
                            raise ValueError("Binary files cannot be previewed as text") from exc
                        diff = "".join(difflib.unified_diff(
                            [], content, fromfile="/dev/null", tofile=f"b/{relative}"
                        ))
                    return 200, {"path": relative, "diff": diff[:500000]}
                return 200, {"diff": _git(self.project_root, "diff", "HEAD", "--", ".", ":(exclude).local-coder/**")[:500000]}
        except (ValueError, TypeError, OSError, UnicodeError):
            return 400, {"error": "Invalid request or path. Check the provider URL, model and selected workspace."}
        return None

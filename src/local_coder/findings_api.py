"""Authenticated workbench adapter for the canonical security evidence lifecycle.

The parent dispatcher supplies authentication. Model work is operator-triggered,
uses the normal configured coordinator and shares the workspace's single job slot.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
from datetime import datetime, timezone
from uuid import uuid4

from local_coder.core.budgets import BudgetExceeded
from local_coder.orchestrator.blocked import AgentBlocked
from local_coder.providers.quota import ProviderQuotaExhausted
from local_coder.security.evidence import EvidenceStore
from local_coder.types import AgentEvent, AgentMode
from local_coder.workspace import Workspace

_FINDING = r"[0-9a-f]{24}"
_JOB = r"[0-9a-f]{32}"
_HISTORY_LIMIT = 40
_MODEL_TIMEOUT = 900
_ACTIVE = {"queued", "running", "cancelling"}


def _body(body, fields):
    if not isinstance(body, dict) or set(body) != set(fields):
        raise ValueError("Supply exactly these fields: " + ", ".join(sorted(fields)))
    return body


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Expected nonempty bounded text")
    return value


class FindingsAPI:
    def _init_findings_api(self):
        self._finding_jobs = {}

    def _findings_handle(self, method, path, body):
        """Return an HTTP response tuple, or None when the route does not match."""
        if path != "/findings" and not path.startswith("/findings/"):
            return None
        try:
            return self._findings_dispatch(method, path, body or {})
        except (ValueError, OSError) as exc:
            return 400, {"error": self._redact(str(exc))}

    def _findings_dispatch(self, method, path, body):
        if method == "GET" and path == "/findings":
            store = EvidenceStore(self.project_root)
            store.sync()
            with self.lock:
                jobs = [copy.deepcopy(row) for row in self._finding_jobs.values()]
            return 200, self._redact({"findings": store.list(), "jobs": jobs})
        match = re.fullmatch(rf"/findings/jobs/({_JOB})(/cancel)?", path)
        if match:
            job_id, cancel = match.groups()
            if (method, bool(cancel)) not in {("GET", False), ("POST", True)}:
                return 404, {"error": "Unknown findings route"}
            if cancel:
                _body(body, set())
            with self.lock:
                row = self._finding_jobs.get(job_id)
                if row is None:
                    return 404, {"error": "Unknown or expired findings job"}
                job = self._jobs.get(job_id)
                if cancel and job and not job["cancel"].is_set():
                    job["cancel"].set()
                    row["status"] = "cancelling"
                    loop, task = job.get("loop"), job.get("task")
                    if loop and task and not loop.is_closed():
                        loop.call_soon_threadsafe(task.cancel)
                result = copy.deepcopy(row)
                if job:
                    result["events"] = copy.deepcopy(job.get("events", []))
            return 200, result
        match = re.fullmatch(rf"/findings/({_FINDING})(?:/(review|prepare|verify|suppress|reopen))?", path)
        if not match:
            return 404, {"error": "Unknown findings route"}
        identity, action = match.groups()
        store = EvidenceStore(self.project_root)
        store.sync()
        try:
            store.get(identity)
        except ValueError:
            return 404, {"error": "Unknown canonical finding"}
        if method == "GET" and action is None:
            return 200, self._redact(next(row for row in store.list() if row["id"] == identity))
        if method != "POST" or action is None:
            return 404, {"error": "Unknown findings route"}
        if action == "verify":
            optional = {'negative_control_ids', 'require_negative_controls'}
            if not isinstance(body, dict):
                raise ValueError('Supply a verification object')
            _body({key: value for key, value in body.items() if key not in optional},
                  {"phase", "test_path", "image", "backend", "timeout"})
            controls = body.get('negative_control_ids', [])
            strict = body.get('require_negative_controls', False)
            if type(strict) is not bool or not isinstance(controls, list) or len(controls) > 20 or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in controls):
                raise ValueError('Use at most 20 bounded negative-control test IDs and a boolean control requirement')
            if len(set(controls)) != len(controls) or (strict and not controls):
                raise ValueError('Strict verification requires distinct negative-control test IDs')
            if body["phase"] not in ("before", "after") or body["backend"] not in ("docker", "podman"):
                raise ValueError("Select before/after and docker/podman")
            if type(body["timeout"]) is not int or not 1 <= body["timeout"] <= 600:
                raise ValueError("Timeout must be an integer from 1 to 600 seconds")
            image = _text(body["image"], 512)
            if image.startswith("-"):
                raise ValueError("Select an already installed trusted image")
            test = _text(body["test_path"], 1000)
            target = Workspace(self.project_root).resolve(test)
            relative = target.relative_to(Workspace(self.project_root).root)
            if not relative.parts or relative.parts[0] != "tests" or target.suffix != ".py" or not target.is_file():
                raise ValueError("Select an existing Python regression under tests/")
        elif action == "suppress":
            _body(body, {"reason"})
            _text(body["reason"], 2000)
        else:
            _body(body, set())
        if action in {"review", "prepare", "verify"}:
            return self._start_finding_job(identity, action, dict(body))
        if not self._job_lock.acquire(blocking=False):
            return 409, {"error": "A task is already running in this workspace"}
        try:
            if self._stopping:
                return 503, {"error": "Engine is shutting down"}
            result = store.suppress(identity, body["reason"]) if action == "suppress" else store.reopen(identity)
            return 200, self._redact(result)
        finally:
            self._job_lock.release()

    def _start_finding_job(self, identity, action, options):
        if not self._job_lock.acquire(blocking=False):
            return 409, {"error": "A task is already running in this workspace"}
        job_id = uuid4().hex
        job = {"cancel": threading.Event(), "loop": None, "task": None, "events": []}
        row = {"job_id": job_id, "session_id": job_id, "finding_id": identity,
               "action": action, "status": "queued", "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            with self.lock:
                if self._stopping:
                    self._job_lock.release()
                    return 503, {"error": "Engine is shutting down"}
                while len(self._finding_jobs) >= _HISTORY_LIMIT:
                    expired = next(key for key, value in self._finding_jobs.items() if value["status"] not in _ACTIVE)
                    del self._finding_jobs[expired]
                self._finding_jobs[job_id] = row
                self._jobs[job_id] = job
            self.sessions.save(job_id, request=f"{action} canonical finding {identity}",
                               mode=AgentMode.CYBERSECURITY.value, phase=action, status="queued")
            thread = threading.Thread(target=self._finding_job_thread, args=(job_id, job, identity, action, options), daemon=True)
            with self.lock:
                if self._stopping:
                    raise ValueError('Engine is shutting down')
                job["thread"] = thread
                thread.start()
            with self.lock:
                return 202, copy.deepcopy(row)
        except BaseException:
            with self.lock:
                self._jobs.pop(job_id, None)
                self._finding_jobs.pop(job_id, None)
            self._job_lock.release()
            raise

    def _finding_job_thread(self, job_id, job, identity, action, options):
        self._context.session_id = job_id
        self._context.mode = AgentMode.CYBERSECURITY
        self._context.goal_id = None

        async def execute():
            coordinator = None
            status, result, error, notice = "failed", None, "", None
            try:
                with self.lock:
                    job["loop"], job["task"] = asyncio.get_running_loop(), asyncio.current_task()
                    if job["cancel"].is_set():
                        raise asyncio.CancelledError()
                    self._finding_jobs[job_id]["status"] = "running"
                self.sessions.save(job_id, status="running")
                self._event(AgentEvent(source="SERVER", event_type="finding_job_started", message=f"Finding {action} started"))
                if action == "verify":
                    store = EvidenceStore(self.project_root)
                    operation = store.verify_controlled if options.get('require_negative_controls') else store.verify
                    control_options = {'negative_control_ids': options.get('negative_control_ids', [])} if (
                        options.get('require_negative_controls') or options.get('negative_control_ids')) else {}
                    result = await operation(identity, phase=options["phase"], test_path=options["test_path"],
                        image=options["image"], backend=options["backend"], timeout_seconds=options["timeout"], **control_options)
                    expected = "failed" if options["phase"] == "before" else "passed"
                    if result["result"]["status"] != expected or (options["phase"] == "before" and result["state"] != "reproduced"):
                        error = "Verification did not establish the requested evidence"
                    if options.get('require_negative_controls') and not result.get(
                            'strict_reproduced' if options['phase'] == 'before' else 'strict_verified'):
                        error = 'Controlled verification did not establish the requested proof stage'
                else:
                    coordinator = self._coordinator()
                    operation = coordinator.review_security_finding if action == "review" else coordinator.prepare_security_regression
                    result = await asyncio.wait_for(operation(identity), timeout=_MODEL_TIMEOUT)
                    if isinstance(result, str):
                        result = result[:64000]
                status = "failed" if error or job.get("failed") else "completed"
            except asyncio.CancelledError:
                status, error = "cancelled", "Stopped; review the workspace for partial changes."
            except AgentBlocked as exc:
                status, error, result = "blocked", str(exc), exc.response.summary
            except ProviderQuotaExhausted as exc:
                status, error, notice = "quota_exhausted", str(exc), exc.notice
            except BudgetExceeded as exc:
                status, error = "budget_exhausted", str(exc)
            except asyncio.TimeoutError:
                status, error = "failed", "Finding operation exceeded its time limit"
            except Exception as exc:
                error = str(exc)
            finally:
                try:
                    if coordinator is not None:
                        close = getattr(coordinator, "close", None)
                        cleanup = asyncio.create_task(asyncio.wait_for(
                            close() if close is not None else coordinator.model_manager.close_all(), timeout=15))
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            status, error = "cancelled", "Stopped; review the workspace for partial changes."
                            await asyncio.shield(cleanup)
                except Exception as exc:
                    status, error = "failed", f"Coordinator cleanup failed: {exc}"
                self._event(AgentEvent(source="SERVER", event_type="finding_job_finished", message=f"Finding {action}: {status}"))
                final = self._redact({"status": status, "result": result, "error": error,
                                      "quota_notice": notice, "events": job.get("events", [])[-600:],
                                      "finished_at": datetime.now(timezone.utc).isoformat()})
                with self.lock:
                    self._finding_jobs[job_id].update(final)
                # The generic sessions UI renders result as report text; findings
                # polling keeps the canonical structured result above.
                report = final["result"]
                if not isinstance(report, str):
                    report = json.dumps(report, indent=2) if report is not None else ""
                self.sessions.save(job_id, **{**final, "result": report})

        try:
            asyncio.run(execute())
        finally:
            with self.lock:
                self._jobs.pop(job_id, None)
            self._job_lock.release()

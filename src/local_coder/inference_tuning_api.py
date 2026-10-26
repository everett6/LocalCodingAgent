"""Authenticated bridge adapter for explicitly requested local inference tuning."""
from __future__ import annotations

import copy
import re
import threading
from uuid import uuid4

from local_coder.inference_tuning import InferenceTuner

_JOB = r"[a-f0-9]{32}"
_ACTIVE = {"queued", "running", "cancelling"}


class InferenceTuningAPI:
    def _init_tuning_api(self):
        if not hasattr(self, "_lifecycle_lock"):
            self._lifecycle_lock = threading.RLock()
        self._tuner = InferenceTuner(self.project_root, self.inference, shutdown=lambda: self._stopping,
                                    lifecycle_gate=self._lifecycle_lock)
        self._tuning_jobs = {}

    def _tuning_get(self, path, query):
        if path == "/inference/tuning/history":
            if query:
                raise ValueError("Tuning history does not accept query fields")
            return True, {"history": self._tuner.history()}
        match = re.fullmatch(rf"/inference/tuning/jobs/({_JOB})", path)
        if match:
            if query:
                raise ValueError("Tuning jobs do not accept query fields")
            with self.lock:
                row = self._tuning_jobs.get(match[1])
                if row is None:
                    raise ValueError("Unknown or expired tuning job")
                return True, copy.deepcopy(row)
        return False, None

    def _tuning_post(self, path, body):
        if path == "/inference/tuning/start":
            self._tuner.options(body)
            if not self._job_lock.acquire(blocking=False):
                raise ValueError("Stop the active workspace job before benchmarking inference")
            job_id = uuid4().hex
            job = {"cancel": threading.Event(), "loop": None, "task": None, "events": []}
            row = {"job_id": job_id, "status": "queued", "result": None}
            try:
                with self.lock:
                    if self._stopping:
                        raise ValueError("Engine is shutting down")
                    while len(self._tuning_jobs) >= 20:
                        key = next(key for key, value in self._tuning_jobs.items() if value["status"] not in _ACTIVE)
                        del self._tuning_jobs[key]
                    self._tuning_jobs[job_id] = row
                    self._jobs[job_id] = job
                    thread = threading.Thread(target=self._tuning_thread, args=(job_id, job, copy.deepcopy(body)), daemon=True, name="inference-tuning")
                    job["thread"] = thread
                    thread.start()
                return True, copy.deepcopy(row)
            except BaseException:
                with self.lock:
                    self._jobs.pop(job_id, None)
                    self._tuning_jobs.pop(job_id, None)
                self._job_lock.release()
                raise
        match = re.fullmatch(rf"/inference/tuning/jobs/({_JOB})/cancel", path)
        if match:
            if body != {}:
                raise ValueError("Cancellation accepts an empty object")
            with self.lock:
                row = self._tuning_jobs.get(match[1])
                if row is None:
                    raise ValueError("Unknown or expired tuning job")
                job = self._jobs.get(match[1])
                if job:
                    job["cancel"].set()
                    row["status"] = "cancelling"
                return True, copy.deepcopy(row)
        if path == "/inference/tuning/apply":
            if not isinstance(body, dict) or set(body) != {"record_id", "variant"} or not isinstance(body["record_id"], str) or not re.fullmatch(_JOB, body["record_id"]) or not isinstance(body["variant"], str) or body["variant"] not in {"context", "gpu", "cache", "draft"}:
                raise ValueError("Choose a measured record_id and variant; supplied metrics are not accepted")
            if not self._job_lock.acquire(blocking=False):
                raise ValueError("Stop the active workspace job before applying inference settings")
            try:
                if self._stopping:
                    raise ValueError("Engine is shutting down")
                return True, self._tuner.apply(body["record_id"], body["variant"])
            finally:
                self._job_lock.release()
        return False, None

    def _tuning_thread(self, job_id, job, options):
        def progress(record):
            with self.lock:
                self._tuning_jobs[job_id].update(status="cancelling" if job["cancel"].is_set() else "running", result=record)
        try:
            with self.lock:
                self._tuning_jobs[job_id]["status"] = "running"
            result = self._tuner.run(options, job["cancel"], progress)
            with self.lock:
                self._tuning_jobs[job_id].update(status=result["status"], result=result)
        except Exception:
            with self.lock:
                self._tuning_jobs[job_id].update(status="failed", error="Local benchmark could not complete; inspect inference configuration and protected history")
        finally:
            with self.lock:
                self._jobs.pop(job_id, None)
            self._job_lock.release()

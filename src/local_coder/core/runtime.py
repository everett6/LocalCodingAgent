"""Durable agent state and full task checkpoints for process restart recovery."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from local_coder.types import AgentMetrics, AgentPhase, AgentResponse, AgentState, AgentTask

logger = logging.getLogger(__name__)


def _write_json(path: Path, data: dict) -> None:
    """Replace atomically so a crash cannot corrupt the last checkpoint."""
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
            name = output.name
            json.dump(data, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class AgentCheckpoint:
    def __init__(self, task_id, iteration, phase, state, metrics, files_changed,
                 files_read, message_history, timestamp, notes=None, task=None):
        self.task_id = task_id
        self.iteration = iteration
        self.phase = phase
        self.state = state
        self.metrics = metrics
        self.files_changed = set(files_changed)
        self.files_read = set(files_read)
        self.message_history = message_history
        self.timestamp = timestamp
        self.notes = notes or {}
        self.task = task

    def to_dict(self) -> dict:
        return {
            "version": 1, "task_id": self.task_id, "iteration": self.iteration,
            "phase": self.phase.value, "state": self.state.model_dump(mode="json"),
            "metrics": self.metrics.model_dump(mode="json"),
            "files_changed": sorted(self.files_changed), "files_read": sorted(self.files_read),
            "message_history": self.message_history, "timestamp": self.timestamp,
            "notes": self.notes, "task": self.task.model_dump(mode="json") if self.task else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentCheckpoint:
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("notes", {}), dict):
            raise ValueError("Unsupported checkpoint version or metadata")
        return cls(
            task_id=data["task_id"], iteration=data["iteration"], phase=AgentPhase(data["phase"]),
            state=AgentState.model_validate(data["state"]),
            metrics=AgentMetrics.model_validate(data["metrics"]),
            files_changed=data["files_changed"], files_read=data["files_read"],
            message_history=data["message_history"], timestamp=data["timestamp"],
            notes=data.get("notes"), task=AgentTask.model_validate(data["task"]) if data.get("task") else None,
        )


class AgentRuntime:
    def __init__(self, project_root: str, checkpoint_interval: int = 5,
                 state_dir: str = ".local-coder/state"):
        if checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be positive")
        self.project_root = str(Path(project_root).resolve())
        self.checkpoint_interval = checkpoint_interval
        self.state_dir = (Path(self.project_root) / state_dir).resolve()
        if not self.state_dir.is_relative_to(Path(self.project_root)) or self.state_dir == Path(self.project_root):
            raise ValueError("State directory must be inside the project workspace")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._active_runs: dict[str, AgentRun] = {}
        self._lock = asyncio.Lock()

    async def start_run(self, task: AgentTask, initial_state: AgentState) -> AgentRun:
        run = AgentRun(uuid.uuid4().hex[:12], task, initial_state, time.time(), self.checkpoint_interval)
        run._runtime = self
        run.state.run_id = run.run_id
        async with self._lock:
            self._active_runs[run.run_id] = run
        return run

    async def end_run(self, run_id: str, final_state: AgentState, success: bool = True,
                      result: AgentResponse | None = None) -> None:
        async with self._lock:
            run = self._active_runs.get(run_id)
        if run:
            await run.persist(final_state, success, result)
            async with self._lock:
                self._active_runs.pop(run_id, None)

    async def get_run(self, run_id: str) -> AgentRun | None:
        async with self._lock:
            return self._active_runs.get(run_id)

    async def list_runs(self) -> list[str]:
        async with self._lock:
            return list(self._active_runs)

    async def release_run(self, run_id: str) -> None:
        """Detach an interrupted run without overwriting its durable boundary."""
        async with self._lock:
            self._active_runs.pop(run_id, None)

    async def checkpoint_run(self, run_id, iteration, phase, state, metrics,
                             files_changed, files_read, message_history, notes=None) -> str | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        checkpoint = AgentCheckpoint(run.task.task_id, iteration, phase, state, metrics,
                                     files_changed, files_read, message_history, time.time(), notes=notes, task=run.task)
        path = self.state_dir / f"checkpoint_{run.run_id}_{iteration}.json"
        await asyncio.to_thread(_write_json, path, checkpoint.to_dict())
        return str(path)

    async def recover_from_checkpoint(self, run_id: str, checkpoint_path: str) -> AgentRun | None:
        try:
            # Run IDs become filenames later; only accept IDs generated by this runtime.
            if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{12}", run_id):
                raise ValueError("Invalid run ID")
            path = Path(checkpoint_path).resolve()
            if path.parent != self.state_dir or not re.fullmatch(rf"checkpoint_{run_id}_[0-9]+\.json", path.name):
                raise ValueError("Checkpoint must be a runtime checkpoint inside the configured state directory")
            data = await asyncio.to_thread(path.read_text, encoding="utf-8")
            checkpoint = AgentCheckpoint.from_dict(json.loads(data))
            if checkpoint.task is None:
                raise ValueError("Legacy checkpoint lacks a full task and cannot resume safely")
            if checkpoint.task_id != checkpoint.task.task_id or checkpoint.state.task_id != checkpoint.task_id:
                raise ValueError("Checkpoint task identity mismatch")
            if checkpoint.state.run_id != run_id:
                raise ValueError("Checkpoint run identity mismatch")
            if checkpoint.phase != checkpoint.state.phase:
                raise ValueError("Checkpoint phase mismatch")
            if checkpoint.iteration != checkpoint.state.iteration or checkpoint.iteration < 0:
                raise ValueError("Checkpoint iteration mismatch")
            if checkpoint.iteration != int(path.stem.rsplit("_", 1)[1]):
                raise ValueError("Checkpoint filename iteration mismatch")
            run = AgentRun(run_id, checkpoint.task, checkpoint.state,
                           checkpoint.timestamp, self.checkpoint_interval)
            run._runtime = self
            run.state.run_id = run_id
            run._iteration = checkpoint.iteration
            run._metrics = checkpoint.metrics
            run._files_changed = checkpoint.files_changed
            run._files_read = checkpoint.files_read
            run._message_history = checkpoint.message_history
            run.notes = checkpoint.notes
            async with self._lock:
                if run_id in self._active_runs:
                    raise ValueError("Run is already active in this runtime")
                self._active_runs[run_id] = run
            return run
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.error("Failed to recover checkpoint: %s", exc)
            return None


class AgentRun:
    def __init__(self, run_id, task, state, created_at, checkpoint_interval):
        self.run_id, self.task, self.state = run_id, task, state
        self.created_at, self.checkpoint_interval = created_at, checkpoint_interval
        self._iteration = state.iteration
        self._files_changed = set(state.files_changed)
        self._files_read = set(state.files_read)
        self._message_history: list = []
        self._last_checkpoint = created_at
        self._metrics = AgentMetrics()
        self.notes: dict = {}

    @property
    def iteration(self):
        return self._iteration

    @property
    def files_changed(self):
        return self._files_changed

    @property
    def files_read(self):
        return self._files_read

    @property
    def message_history(self):
        return self._message_history

    @property
    def metrics(self):
        return self._metrics

    def validate_resume(self) -> None:
        """Reject incomplete protocol histories and impossible counter resets."""
        if self.notes.get("boundary_complete") is not True:
            raise ValueError("Checkpoint is not a complete agent round")
        if self.state.phase in {AgentPhase.DONE, AgentPhase.FAILED}:
            raise ValueError("Checkpoint is already terminal")
        state = self.state
        if not (0 <= state.iteration <= state.max_iterations
                and 0 <= state.tool_calls_used <= state.max_tool_calls
                and 0 <= state.test_runs <= state.max_test_runs):
            raise ValueError("Checkpoint counters exceed their saved limits")
        if self.metrics.tool_calls != state.tool_calls_used or self.metrics.model_calls != state.iteration:
            raise ValueError("Checkpoint metrics disagree with saved counters")
        if len(state.tool_calls) != state.tool_calls_used:
            raise ValueError("Checkpoint tool history disagrees with saved counters")
        if len(state.test_results) != state.test_runs:
            raise ValueError("Checkpoint verification history disagrees with saved counters")
        pending: set[str] = set()
        for message in state.messages:
            if message.role == "assistant":
                if pending:
                    raise ValueError("Checkpoint contains unresolved tool calls")
                ids = [tc.id for tc in message.tool_calls or []]
                if len(ids) != len(set(ids)):
                    raise ValueError("Checkpoint contains duplicate tool call IDs")
                pending = set(ids)
            elif message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("Checkpoint contains an unmatched tool result")
                pending.remove(message.tool_call_id)
        if pending:
            raise ValueError("Checkpoint contains unresolved tool calls")

    @property
    def pending_batch_path(self) -> Path:
        return self._runtime.state_dir / f"run_{self.run_id}_pending.json"

    async def begin_tool_batch(self, tool_calls) -> None:
        # This journal is deliberately not a resumable conversation checkpoint.
        # A crash between a tool side effect and its response has ambiguous
        # effects; refuse to replay that batch automatically on restart.
        await asyncio.to_thread(_write_json, self.pending_batch_path, {
            "iteration": self.state.iteration,
            "tools": [{"id": tc.id, "name": tc.name} for tc in tool_calls],
        })

    async def validate_pending_batch(self) -> None:
        path = self.pending_batch_path
        if not path.exists():
            return
        if path.resolve().parent != self._runtime.state_dir:
            raise ValueError("Pending batch journal is outside the state directory")
        pending = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        if not isinstance(pending, dict) or type(pending.get("iteration")) is not int or pending["iteration"] < 1:
            raise ValueError("Invalid pending tool batch journal")
        if pending["iteration"] > self.iteration:
            raise ValueError("Interrupted tool batch may have changed the workspace; inspect its effects before starting a new task")
        # A newer full checkpoint proves the batch completed even if the
        # process stopped before deleting the journal.
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def checkpoint_boundary(self, state: AgentState, metrics: AgentMetrics,
                                  notes: dict) -> str:
        """Save a complete model/tool round; tool calls in the batch have all returned."""
        self.state = state
        self._iteration = state.iteration
        self._metrics = metrics.model_copy(deep=True)
        self._files_changed = set(state.files_changed)
        self._files_read = set(state.files_read)
        self._message_history = [message.model_dump(mode="json") for message in state.messages]
        self.notes = notes
        path = await self._runtime.checkpoint_run(
            self.run_id, self._iteration, state.phase, state, self._metrics,
            self._files_changed, self._files_read, self._message_history, notes=notes,
        )
        if path is None:
            raise RuntimeError("Cannot checkpoint an inactive run")
        await asyncio.to_thread(self.pending_batch_path.unlink, missing_ok=True)
        self._last_checkpoint = time.time()
        return path

    async def record_tool_call(self, tool_name, success, output, error=None):
        self._iteration += 1
        if isinstance(output, dict):
            self._files_changed.update(output.get("files_changed", []))
            self._files_read.update(output.get("files_read", []))
        self._metrics.tool_calls += 1
        self._message_history.append({"iteration": self._iteration, "tool": tool_name,
                                      "success": success, "error": error, "timestamp": time.time()})
        if self._iteration % self.checkpoint_interval == 0:
            await self._create_checkpoint()

    def _update_state(self):
        self.state.iteration = self._iteration
        self.state.tool_calls_used = self._metrics.tool_calls
        self.state.files_changed = set(self._files_changed)
        self.state.files_read = set(self._files_read)
        self._metrics.files_read = len(self._files_read)
        self._metrics.files_written = len(self._files_changed)

    async def _create_checkpoint(self):
        self._update_state()
        await self._runtime.checkpoint_run(self.run_id, self._iteration, self.state.phase,
                                          self.state, self._metrics, self._files_changed,
                                          self._files_read, self._message_history)
        self._last_checkpoint = time.time()

    async def persist(self, final_state, success, result=None):
        self.state = final_state
        # Agent execution owns model-round counters. record_tool_call() callers
        # still use the legacy accumulator, which already updates state itself.
        if result is None:
            self._update_state()
        else:
            self._iteration = final_state.iteration
            self._files_changed = set(final_state.files_changed)
            self._files_read = set(final_state.files_read)
        self.state.phase = AgentPhase.DONE if success else AgentPhase.FAILED
        self.state.finished_at = datetime.now()
        data = self.state.model_dump(mode="json")
        if result is not None:
            data["result"] = result.model_dump(mode="json")
        await asyncio.to_thread(_write_json, self._runtime.state_dir / f"run_{self.run_id}_final.json",
                                data)


def create_runtime(project_root: str, **kwargs) -> AgentRuntime:
    return AgentRuntime(project_root, **kwargs)

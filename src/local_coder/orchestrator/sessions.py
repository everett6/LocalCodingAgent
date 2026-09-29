"""Small JSON session journal used for resume and remote status."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import fcntl


class SessionStore:
    def __init__(self, project_root: str, state_dir: str = ".local-coder"):
        self.path = Path(project_root) / state_dir / "sessions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self.lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _read(self) -> dict[str, dict[str, Any]]:
        with self._lock():
            return self._read_unlocked()

    def save(self, session_id: str, **data: Any) -> dict[str, Any]:
        with self._lock():
            sessions = self._read_unlocked()
            record = sessions.get(session_id, {})
            record.update(data)
            record["session_id"] = session_id
            record["updated_at"] = datetime.now().isoformat()
            sessions[session_id] = record
            payload = json.dumps(sessions, indent=2, default=str)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = temporary.name
            os.replace(temporary_path, self.path)
        return record

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self._read().get(session_id)

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._read().values(), key=lambda item: item.get("updated_at", ""), reverse=True)

    @property
    def sessions_dir(self) -> Path:
        return self.path.parent / "sessions"

    def create(self, session_id: str | None = None) -> Session:
        session_id = validate_session_id(session_id or new_session_id())
        if (self.sessions_dir / session_id / "state.json").exists():
            raise SessionError(f"Session already exists: {session_id}")
        now = _now()
        session = Session(self, session_id, {
            "version": 1, "session_id": session_id, "created_at": now, "updated_at": now, "turns": [],
        })
        session.dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(session.state_path, session.state)
        return session

    def open(self, session_id: str) -> Session:
        validate_session_id(session_id)
        path = self.sessions_dir / session_id / "state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SessionError(f"Session not found: {session_id}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"Session {session_id} could not be read: {exc}") from exc
        state.setdefault("turns", [])
        return Session(self, session_id, state)

    def open_or_create(self, session_id: str) -> Session:
        try:
            return self.open(session_id)
        except SessionError:
            if (self.sessions_dir / validate_session_id(session_id) / "state.json").exists():
                raise
            return self.create(session_id)

    def latest(self) -> Session | None:
        """The most recently updated persistent session, if any."""
        for record in self.list():
            if record.get("persistent") and (self.sessions_dir / record["session_id"] / "state.json").exists():
                return self.open(record["session_id"])
        return None


# === Persistent sessions ===
#
# A session is a folder under <state_dir>/sessions/<session_id>/ holding:
#   state.json   -- every turn (request, status, per-phase checkpoints, report)
#   events.jsonl -- append-only log of the agent events emitted while running
#   .lock        -- held while a process is running a turn in this session
#
# The Coordinator writes a checkpoint after each phase (explore, plan, each
# finished plan task, review, every test run and fix attempt), so a run that
# crashes, errors, or is interrupted picks up after the last finished phase
# instead of starting cold. A follow-up request in the same session gets the
# earlier turns' requests and results as context.

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
UNFINISHED_STATUSES = ("running", "interrupted", "failed")
HISTORY_TURNS = 5
HISTORY_RESULT_CHARS = 1500


class SessionError(Exception):
    """A session could not be found, opened, or locked."""


def new_session_id() -> str:
    return f"local-{uuid.uuid4().hex[:8]}"


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.match(session_id or "") or ".." in session_id:
        raise SessionError(
            f"Invalid session id {session_id!r}: use letters, digits, '-', '_' or '.' (max 64 chars)"
        )
    return session_id


def _write_json_atomic(path: Path, payload: Any) -> None:
    data = json.dumps(payload, indent=2, default=str)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write(data)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = temporary.name
    os.replace(temporary_path, path)


def _now() -> str:
    return datetime.now().isoformat()


class TurnCheckpoint:
    """get/put view of one turn's phase checkpoints, handed to
    Coordinator.run. Every put is written to disk before returning, so the
    phase that just finished survives a crash right after it."""

    def __init__(self, session: "Session", turn: dict[str, Any]):
        self._session = session
        self._turn = turn

    def get(self, key: str) -> Any | None:
        return self._turn["checkpoints"].get(key)

    def put(self, key: str, value: Any) -> None:
        self._turn["checkpoints"][key] = value
        self._turn["phase"] = key
        self._session.save()

    def keys(self) -> list[str]:
        return list(self._turn["checkpoints"])


class Session:
    """One persistent, resumable conversation with the agent."""

    def __init__(self, store: SessionStore, session_id: str, state: dict[str, Any]):
        self.store = store
        self.session_id = session_id
        self.dir = store.sessions_dir / session_id
        self.state = state
        self._lock_file = None

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def turns(self) -> list[dict[str, Any]]:
        return self.state["turns"]

    def last_turn(self) -> dict[str, Any] | None:
        return self.turns[-1] if self.turns else None

    def unfinished_turn(self) -> dict[str, Any] | None:
        """The last turn, if it never completed (crashed, errored, or was
        interrupted) -- the one `--resume` picks back up."""
        turn = self.last_turn()
        if turn is not None and turn["status"] in UNFINISHED_STATUSES:
            return turn
        return None

    def history_context(self) -> str:
        """Earlier completed turns, condensed, for a follow-up request."""
        finished = [turn for turn in self.turns if turn["status"] == "completed"][-HISTORY_TURNS:]
        if not finished:
            return ""
        lines = ["Earlier in this session:"]
        for turn in finished:
            result = (turn.get("report") or "").strip()
            if len(result) > HISTORY_RESULT_CHARS:
                result = result[:HISTORY_RESULT_CHARS] + "\n[... truncated]"
            lines.append(f"\n## Request {turn['index'] + 1}: {turn['request']}\nResult:\n{result}")
        return "\n".join(lines)

    def start_turn(self, request: str, phase: str = "run") -> dict[str, Any]:
        previous = self.unfinished_turn()
        if previous is not None:
            previous["status"] = "superseded"
        turn = {
            "index": len(self.turns),
            "request": request,
            "kind": phase,
            "context": self.history_context(),
            "status": "running",
            "phase": None,
            "started_at": _now(),
            "finished_at": None,
            "checkpoints": {},
            "report": None,
            "error": None,
            "attempts": 1,
        }
        self.turns.append(turn)
        self.save()
        return turn

    def reopen_turn(self, turn: dict[str, Any]) -> dict[str, Any]:
        turn["status"] = "running"
        turn["error"] = None
        turn["attempts"] = turn.get("attempts", 1) + 1
        self.save()
        return turn

    def checkpoint(self, turn: dict[str, Any]) -> TurnCheckpoint:
        return TurnCheckpoint(self, turn)

    def finish_turn(self, turn: dict[str, Any], status: str, report: str | None = None, error: str | None = None) -> None:
        turn["status"] = status
        turn["finished_at"] = _now()
        if report is not None:
            turn["report"] = report
        turn["error"] = error
        self.save()

    def record_event(self, event: Any) -> None:
        """Agent event handler: append the event to events.jsonl."""
        payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
        turn = self.last_turn()
        if turn is not None:
            payload["turn"] = turn["index"]
        try:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
        except OSError:
            pass  # the event log is best-effort; checkpoints are what resume needs

    def save(self) -> None:
        self.state["updated_at"] = _now()
        self.dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.state_path, self.state)
        turn = self.last_turn()
        self.store.save(
            self.session_id,
            request=turn["request"] if turn else "",
            phase=turn["kind"] if turn else "run",
            status=turn["status"] if turn else "empty",
            result=turn.get("report") if turn else None,
            turns=len(self.turns),
            persistent=True,
        )

    # -- single-writer lock, so two terminals can't run the same session --

    def acquire(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        lock_file = (self.dir / ".lock").open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            raise SessionError(f"Session {self.session_id} is already running in another process")
        self._lock_file = lock_file
        # Another process may have run this session since it was opened.
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.state.setdefault("turns", [])

    def release(self) -> None:
        if self._lock_file is not None:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def __enter__(self) -> "Session":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()

"""Small JSON session journal used for resume and remote status."""
from __future__ import annotations

import json
import copy
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from local_coder.file_lock import exclusive_lock
from local_coder.context.source_snapshot import read_source_snapshot
from local_coder.statedir import state_path
from local_coder.workspace import Workspace


class SessionStore:
    def __init__(self, project_root: str, state_dir: str = ".local-coder"):
        self.root = Workspace(project_root).root
        relative = Path(state_dir)
        if relative.is_absolute():
            try:
                relative = relative.relative_to(self.root)
            except ValueError:
                raise SessionError("Session state must remain inside the workspace") from None
        if ".." in relative.parts:
            raise SessionError("Session state must remain inside the workspace")
        self.state_root = self.root / relative
        self.path = self.state_root / "sessions.json"
        self.lock_path = self.path.with_suffix(".lock")
        self._journal_cache: dict | None = None
        self._journal_cache_identity: tuple[int, ...] | None = None
        self._guard(self.path, create=True)

    def _clear_journal_cache(self) -> None:
        self._journal_cache = None
        self._journal_cache_identity = None

    def _journal_fingerprint(self) -> tuple[int, ...]:
        """Check current regular-file identity without reading its contents."""
        self._guard(self.path)
        descriptors = []
        try:
            relative = self.path.relative_to(self.root)
            if hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptors.append(os.open(self.root, flags))
                for part in relative.parts[:-1]:
                    descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
                descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=descriptors[-1])
            else:
                descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != "nt" and info.st_uid != os.getuid()):
                raise SessionError("Session journal must remain an owned regular file")
            self._guard(self.path)
            current = self.path.stat(follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            identity = tuple(getattr(info, field) for field in fields)
            if identity != tuple(getattr(current, field) for field in fields):
                raise SessionError("Session journal changed while checking its identity")
            return identity
        except FileNotFoundError:
            raise
        except OSError:
            raise SessionError("Session journal identity could not be checked safely") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _guard(self, path: Path, *, create: bool = False) -> None:
        try:
            relative = path.relative_to(self.root)
            if ".." in relative.parts:
                raise ValueError("Traversal")
            if create:
                state_path(self.root, relative)
            current = self.root
            for part in relative.parts[:-1]:
                current /= part
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    if not create:
                        return
                    raise
                if not stat.S_ISDIR(info.st_mode) or (os.name != "nt" and info.st_uid != os.getuid()):
                    raise ValueError("Unsafe state directory")
                if create and current.is_relative_to(self.state_root):
                    current.chmod(0o700)
            try:
                info = path.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != "nt" and info.st_uid != os.getuid()):
                raise ValueError("Unsafe state file")
        except (OSError, ValueError):
            raise SessionError("Session storage must use owned regular files and directories without links") from None

    def _open_private(self, path: Path, mode: str):
        self._guard(path, create=mode != "rb")
        flags = os.O_RDONLY if mode == "rb" else os.O_CREAT | os.O_APPEND | (os.O_RDWR if mode == "a+" else os.O_WRONLY)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = None
        try:
            descriptor = os.open(path, flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != "nt" and info.st_uid != os.getuid()):
                raise SessionError("Session storage must use private regular files")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, mode, **({} if mode == "rb" else {"encoding": "utf-8"}))
            descriptor = None
            return handle
        except OSError:
            raise SessionError("Session storage could not be opened safely; existing files were preserved") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read_json(self, path: Path, limit: int) -> Any:
        self._guard(path)
        try:
            snapshot = read_source_snapshot(self.root, path, max_bytes=limit)
            return json.loads(snapshot.data.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
        except FileNotFoundError:
            raise
        except (OSError, ValueError, RecursionError):
            raise SessionError("Session data is unreadable, invalid or too large; preserve the file and restore a valid copy") from None

    def _write(self, path: Path, payload: Any, limit: int) -> None:
        if path == self.path:
            self._clear_journal_cache()
        self._guard(path, create=True)
        _write_json_atomic(path, payload, max_bytes=limit, guard=lambda: self._guard(path, create=True))

    @contextmanager
    def _lock(self) -> Iterator[None]:
        try:
            with self._open_private(self.lock_path, "a+") as lock_file:
                with exclusive_lock(lock_file):
                    self._guard(self.lock_path)
                    if self.lock_path.stat(follow_symlinks=False).st_ino != os.fstat(lock_file.fileno()).st_ino:
                        raise SessionError("Session journal lock changed while acquiring it")
                    yield
        except BaseException:
            self._clear_journal_cache()
            raise

    def _read_unlocked(self, *, copy_result: bool = True) -> dict[str, dict[str, Any]]:
        """Return validated state; borrowed cache data must stay under the lock."""
        observed_existing = False
        try:
            for _ in range(2):
                try:
                    identity = self._journal_fingerprint()
                except FileNotFoundError:
                    self._clear_journal_cache()
                    if observed_existing:
                        raise SessionError("Session journal disappeared while reading; retain saved history for recovery") from None
                    return {}
                observed_existing = True
                if (identity[2] <= MAX_JOURNAL_CACHE_BYTES and self._journal_cache is not None
                        and identity == self._journal_cache_identity):
                    sessions = copy.deepcopy(self._journal_cache) if copy_result else self._journal_cache
                    if self._journal_fingerprint() == identity:
                        return sessions
                    self._clear_journal_cache()
                    continue
                self._clear_journal_cache()
                try:
                    sessions = self._read_json(self.path, MAX_JOURNAL_BYTES)
                    _validate_index(sessions)
                    latest = self._journal_fingerprint()
                except FileNotFoundError:
                    continue
                if latest != identity:
                    continue
                if identity[2] <= MAX_JOURNAL_CACHE_BYTES:
                    self._journal_cache, self._journal_cache_identity = sessions, identity
                    return copy.deepcopy(sessions) if copy_result else sessions
                return sessions
            raise SessionError("Session journal changed repeatedly while reading; retry after other writers finish")
        except BaseException:
            self._clear_journal_cache()
            raise

    def _read(self) -> dict[str, dict[str, Any]]:
        with self._lock():
            return self._read_unlocked()

    def save(self, session_id: str, **data: Any) -> dict[str, Any]:
        validate_session_id(session_id)
        with self._lock():
            sessions = self._read_unlocked()
            return self._save_unlocked(sessions, session_id, data)

    def _save_unlocked(self, sessions: dict, session_id: str, data: dict) -> dict:
        record = {**sessions.get(session_id, {}), **data, "session_id": session_id, "updated_at": _now()}
        sessions[session_id] = record
        _validate_index(sessions)
        self._write(self.path, sessions, MAX_JOURNAL_BYTES)
        return record

    def get(self, session_id: str) -> dict[str, Any] | None:
        validate_session_id(session_id)
        with self._lock():
            # Copy only the requested record; an unrelated large report should
            # not be copied every time the active task's status is polled.
            return copy.deepcopy(self._read_unlocked(copy_result=False).get(session_id))

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._read().values(), key=lambda item: item.get("updated_at", ""), reverse=True)

    @property
    def sessions_dir(self) -> Path:
        return self.path.parent / "sessions"

    def create(self, session_id: str | None = None) -> Session:
        session_id = validate_session_id(new_session_id() if session_id is None else session_id)
        with self._lock():
            self._read_unlocked()  # Never create new state over a damaged journal.
            path = self.sessions_dir / session_id / "state.json"
            self._guard(path)
            if path.exists():
                raise SessionError(f"Session already exists: {session_id}")
            now = _now()
            session = Session(self, session_id, {
                "version": 1, "session_id": session_id, "created_at": now, "updated_at": now, "turns": [],
            })
            self._write(session.state_path, session.state, MAX_SESSION_BYTES)
            return session

    def _read_state(self, session_id: str) -> dict:
        validate_session_id(session_id)
        try:
            state = self._read_json(self.sessions_dir / session_id / "state.json", MAX_SESSION_BYTES)
        except FileNotFoundError:
            raise _SessionNotFound(f"Session not found: {session_id}") from None
        _validate_state(state, session_id)
        return state

    def open(self, session_id: str) -> Session:
        return Session(self, session_id, self._read_state(session_id))

    def open_or_create(self, session_id: str) -> Session:
        try:
            return self.open(session_id)
        except _SessionNotFound:
            return self.create(session_id)

    def latest(self) -> Session | None:
        """The most recently updated persistent session, if any."""
        for record in self.list():
            if record.get("persistent"):
                try:
                    return self.open(record["session_id"])
                except _SessionNotFound:
                    continue
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
HISTORY_REQUEST_CHARS = 2000
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
# Windows ctime currently represents creation time, so restored-mtime edits do
# not support this metadata invalidation contract there. Keep reads uncached.
MAX_JOURNAL_CACHE_BYTES = 0 if os.name == "nt" else 8 * 1024 * 1024
MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_EVENT_FILE_BYTES = 32 * 1024 * 1024
EVENT_TAIL_BYTES = 4 * 1024 * 1024
_EVENT_SECRET_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
                               r"password|passwd|authorization|credentials?|private[_-]?key|token|secret)", re.I)


class SessionError(Exception):
    """A session could not be found, opened, or locked."""


class _SessionNotFound(SessionError):
    """Only genuinely absent state may trigger open-or-create."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON value")


def _redact_event(payload: dict) -> dict:
    """Scrub known credential formats and exact credential fields, not usage."""
    from local_coder.integrations.mcp import redact_data
    from local_coder.memory.context_store import _clean
    secrets = set()

    def collect(value, sensitive=False):
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, sensitive or bool(_EVENT_SECRET_KEY.fullmatch(str(key))))
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item, sensitive)
        elif sensitive and isinstance(value, str) and len(value) >= 4:
            secrets.add(value)

    def hide_fields(value):
        if isinstance(value, dict):
            return {key: "[redacted]" if _EVENT_SECRET_KEY.fullmatch(str(key)) else hide_fields(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [hide_fields(item) for item in value]
        if isinstance(value, str):
            return _clean(value)
        return value

    collect(payload)
    return hide_fields(redact_data(payload, tuple(sorted(secrets, key=len, reverse=True))))


def _validate_index(sessions: Any) -> None:
    if not isinstance(sessions, dict):
        raise SessionError("Session journal must contain an object; existing history was preserved")
    for session_id, record in sessions.items():
        validate_session_id(session_id)
        if not isinstance(record, dict) or record.get("session_id") != session_id:
            raise SessionError("Session journal has invalid records; existing history was preserved")
        if not isinstance(record.get("updated_at"), str):
            raise SessionError("Session journal has invalid timestamps; existing history was preserved")
        for key in ("request", "phase", "status", "mode", "checkpoint", "result", "error"):
            if record.get(key) is not None and not isinstance(record[key], str):
                raise SessionError("Session journal has invalid field types; existing history was preserved")
        if "persistent" in record and type(record["persistent"]) is not bool:
            raise SessionError("Session journal has invalid persistence flags; existing history was preserved")
        if "turns" in record and (type(record["turns"]) is not int or record["turns"] < 0):
            raise SessionError("Session journal has invalid turn counts; existing history was preserved")
        if "events" in record and (not isinstance(record["events"], list) or any(not isinstance(item, dict) for item in record["events"])):
            raise SessionError("Session journal has invalid events; existing history was preserved")


def _validate_state(state: Any, session_id: str) -> None:
    if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1
            or state.get("session_id") != session_id or not isinstance(state.get("turns"), list)
            or any(not isinstance(state.get(key), str) for key in ("created_at", "updated_at"))):
        raise SessionError("Persistent session has an invalid schema; existing state was preserved")
    for index, turn in enumerate(state["turns"]):
        if (not isinstance(turn, dict) or type(turn.get("index")) is not int or turn["index"] != index
                or not isinstance(turn.get("request"), str) or not isinstance(turn.get("kind"), str)
                or not isinstance(turn.get("status"), str) or not isinstance(turn.get("checkpoints"), dict)
                or not isinstance(turn.get("context", ""), str)):
            raise SessionError("Persistent session has invalid turn records; existing state was preserved")
        for key in ("phase", "started_at", "finished_at", "report", "error"):
            if turn.get(key) is not None and not isinstance(turn[key], str):
                raise SessionError("Persistent session has invalid turn fields; existing state was preserved")
        if "attempts" in turn and (type(turn["attempts"]) is not int or turn["attempts"] < 1):
            raise SessionError("Persistent session has invalid attempt counts; existing state was preserved")


def new_session_id() -> str:
    return f"local-{uuid.uuid4().hex[:8]}"


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id) or ".." in session_id:
        raise SessionError(
            "Invalid session id: use letters, digits, '-', '_' or '.' (max 64 chars)"
        )
    return session_id


def _json_bytes(payload: Any, max_bytes: int) -> bytes:
    try:
        data = json.dumps(payload, indent=2, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise SessionError("Session data must contain finite JSON values; existing files were preserved") from None
    if len(data) > max_bytes:
        raise SessionError("Session data exceeds the storage byte limit; existing files were preserved")
    return data


def _write_json_atomic(path: Path, payload: Any, *, max_bytes: int, guard) -> None:
    data = _json_bytes(payload, max_bytes)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".session-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        guard()
        os.replace(temporary_path, path)
    except OSError:
        raise SessionError("Session snapshot could not be saved; retain the existing files for recovery") from None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
        self._lease = None
        self._events_omitted = False

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def events_status_path(self) -> Path:
        return self.dir / "events-status.json"

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
            request = turn["request"]
            if len(request) > HISTORY_REQUEST_CHARS:
                request = request[:HISTORY_REQUEST_CHARS] + "\n[... request truncated; full turn retained in session state]"
            lines.append(f"\n## Request {turn['index'] + 1}: {request}\nResult:\n{result}")
        return "\n".join(lines)

    def progress(self, turn_index: int | None = None) -> dict[str, Any] | None:
        """Return a structured view of saved phases and DAG task transitions."""
        if not self.turns:
            return None
        if turn_index is None:
            turn = self.last_turn()
        else:
            if isinstance(turn_index, bool) or not isinstance(turn_index, int) or turn_index < 0:
                raise ValueError("Turn index must be a non-negative integer")
            turn = next((item for item in self.turns if item.get("index") == turn_index), None)
            if turn is None:
                raise SessionError(f"Turn not found: {turn_index}")

        assert turn is not None
        index = turn["index"]
        checkpoints = turn.get("checkpoints", {})
        current_phase = turn.get("phase")
        planned_tasks: dict[str, dict[str, Any]] = {}
        task_states: dict[str, dict[str, Any]] = {}

        plan = checkpoints.get("plan")
        if isinstance(plan, dict):
            for item in plan.get("tasks", []):
                if isinstance(item, dict) and isinstance(item.get("task_id"), str):
                    planned_tasks[item["task_id"]] = item
        plan_events = []
        latest_status: dict[str, dict[str, Any]] = {}
        observed_phase = None
        event_lines, events_truncated = self._event_tail()
        for line in event_lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("turn") != index:
                continue
            if event.get("event_type") == "phase" and isinstance(event.get("message"), str):
                observed_phase = event.get("message") or observed_phase
            elif event.get("event_type") == "plan_created":
                data = event.get("data")
                if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                    plan_events = data["tasks"]
            elif event.get("event_type") == "task_status":
                data = event.get("data")
                if isinstance(data, dict) and isinstance(data.get("task_id"), str):
                    latest_status[data["task_id"]] = data

        # An omitted later event may have advanced beyond the retained phase.
        # Keep that observation visible, but never let incomplete event history
        # move the authoritative saved checkpoint backwards.
        if not events_truncated and observed_phase is not None:
            current_phase = observed_phase

        if not planned_tasks:
            for item in plan_events:
                if isinstance(item, dict) and isinstance(item.get("task_id"), str):
                    planned_tasks[item["task_id"]] = item

        for task_id, task in planned_tasks.items():
            saved = checkpoints.get(f"task.{task_id}")
            status_data = latest_status.get(task_id, {})
            status = status_data.get("status", "pending")
            summary = status_data.get("note", "")
            if isinstance(saved, dict):
                status = saved.get("status", status)
                summary = saved.get("summary", summary)
            task_states[task_id] = {
                "task_id": task_id,
                "role": task.get("role", ""),
                "objective": task.get("objective", ""),
                "status": status,
                "summary": summary,
            }

        if not planned_tasks and isinstance(checkpoints.get("execute"), dict):
            result = checkpoints["execute"]
            task_id = result.get("task_id", "execute")
            task_states[task_id] = {
                "task_id": task_id,
                "role": "coder",
                "objective": turn.get("request", ""),
                "status": result.get("status", "unknown"),
                "summary": result.get("summary", ""),
            }

        phase_names = ("explore", "plan", "execute", "review", "tests.0")
        completed_phases = [name for name in phase_names if name in checkpoints]
        completed_phases.extend(
            name for name in checkpoints
            if name.startswith(("tests.", "fix.")) and name not in completed_phases
        )
        return {
            "session_id": self.session_id,
            "turn_index": index,
            "request": turn.get("request", ""),
            "status": turn.get("status", "unknown"),
            "current_phase": current_phase,
            "observed_phase": observed_phase,
            "completed_phases": completed_phases,
            "tasks": list(task_states.values()),
            "task_counts": {
                status: sum(item["status"] == status for item in task_states.values())
                for status in ("pending", "running", "completed", "failed")
            },
            "error": turn.get("error"),
            "report_available": bool(turn.get("report")),
            "updated_at": turn.get("finished_at") or turn.get("started_at"),
            "event_history_truncated": events_truncated,
        }

    def _event_tail(self) -> tuple[list[str], bool]:
        """Read bounded recent observations; checkpoints remain authoritative."""
        self.store._guard(self.events_path)
        omissions = self._event_omissions()
        if not self.events_path.exists():
            return [], self._events_omitted or omissions is not None
        with self.store._open_private(self.events_path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            offset = max(0, size - EVENT_TAIL_BYTES)
            handle.seek(offset)
            text = handle.read(EVENT_TAIL_BYTES).decode("utf-8", errors="replace")
        lines = text.splitlines()
        return (lines[1:] if offset else lines), bool(offset or self._events_omitted or omissions is not None)

    def _event_omissions(self) -> dict | None:
        try:
            record = self.store._read_json(self.events_status_path, 4096)
        except FileNotFoundError:
            return None
        if (not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1
                or type(record.get("omitted_events_at_least")) is not int or record["omitted_events_at_least"] < 1
                or record.get("reason") not in {"event_file_limit", "event_byte_limit", "event_not_recorded"}
                or not isinstance(record.get("updated_at"), str)):
            raise SessionError("Event omission metadata is invalid; preserve it for inspection")
        return record

    def _mark_event_omission(self, reason: str) -> None:
        # Keep an explicit durable lower bound without repeatedly rewriting logs
        # or pretending dropped best-effort events can be counted exactly.
        self._events_omitted = True
        try:
            if self._event_omissions() is None:
                self.store._write(self.events_status_path, {
                    "version": 1, "omitted_events_at_least": 1, "reason": reason, "updated_at": _now(),
                }, 4096)
        except (OSError, SessionError):
            pass

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
            _json_bytes(payload, MAX_EVENT_BYTES)
            payload = _redact_event(payload)
            data = json.dumps(payload, allow_nan=False) + "\n"
            if len(data.encode("utf-8")) > MAX_EVENT_BYTES:
                self._mark_event_omission("event_byte_limit")
                return
            with self.store._open_private(self.events_path, "a") as handle:
                if os.fstat(handle.fileno()).st_size + len(data.encode("utf-8")) > MAX_EVENT_FILE_BYTES:
                    self._mark_event_omission("event_file_limit")
                    return
                handle.write(data)
        except (OSError, SessionError, TypeError, ValueError, RecursionError):
            self._mark_event_omission("event_not_recorded")
            # The event log is best-effort; checkpoints are what resume needs.

    def save(self) -> None:
        self.state["updated_at"] = _now()
        _validate_state(self.state, self.session_id)
        turn = self.last_turn()
        data = dict(
            request=turn["request"] if turn else "",
            phase=turn["kind"] if turn else "run",
            status=turn["status"] if turn else "empty",
            result=turn.get("report") if turn else None,
            turns=len(self.turns),
            persistent=True,
        )
        with self.store._lock():
            sessions = self.store._read_unlocked()
            # Detect damage after open/acquire before replacing any saved state.
            self.store._read_state(self.session_id)
            candidate = {**sessions, self.session_id: {**sessions.get(self.session_id, {}), **data,
                         "session_id": self.session_id, "updated_at": _now()}}
            _validate_index(candidate)
            # Preflight both serialized limits before replacing either file.
            _json_bytes(self.state, MAX_SESSION_BYTES)
            _json_bytes(candidate, MAX_JOURNAL_BYTES)
            self.store._write(self.state_path, self.state, MAX_SESSION_BYTES)
            self.store._save_unlocked(sessions, self.session_id, data)

    # -- single-writer lock, so two terminals can't run the same session --

    def acquire(self) -> None:
        if self._lock_file is not None:
            raise SessionError(f"Session {self.session_id} is already running in this process")
        lock_file = self.store._open_private(self.dir / ".lock", "a+")
        lease = exclusive_lock(lock_file, blocking=False)
        try:
            lease.__enter__()
        except OSError:
            lock_file.close()
            raise SessionError(f"Session {self.session_id} is already running in another process")
        try:
            self.store._guard(self.dir / ".lock")
            if (self.dir / ".lock").stat(follow_symlinks=False).st_ino != os.fstat(lock_file.fileno()).st_ino:
                raise SessionError("Session writer lock changed while acquiring it")
            # Another process may have run this session since it was opened.
            self.state = self.store._read_state(self.session_id)
        except BaseException:
            lease.__exit__(None, None, None)
            lock_file.close()
            raise
        self._lock_file = lock_file
        self._lease = lease

    def release(self) -> None:
        if self._lock_file is not None:
            try:
                self._lease.__exit__(None, None, None)
            finally:
                self._lock_file.close()
                self._lock_file = None
                self._lease = None

    def __enter__(self) -> "Session":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()

"""Small JSON session journal used for resume and remote status."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from typing import Any

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
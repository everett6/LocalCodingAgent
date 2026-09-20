"""Small JSON session journal used for resume and remote status."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, project_root: str, state_dir: str = ".local-coder"):
        self.path = Path(project_root) / state_dir / "sessions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, session_id: str, **data: Any) -> dict[str, Any]:
        sessions = self._read()
        record = sessions.get(session_id, {})
        record.update(data)
        record["session_id"] = session_id
        record["updated_at"] = datetime.now().isoformat()
        sessions[session_id] = record
        self.path.write_text(json.dumps(sessions, indent=2, default=str), encoding="utf-8")
        return record

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self._read().get(session_id)

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._read().values(), key=lambda item: item.get("updated_at", ""), reverse=True)
"""Memory systems for project and task state."""
from __future__ import annotations

import json
import logging
import sqlite3
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MemoryStore:
    """SQLite-backed persistent memory for project and task state.

    Provides three levels of memory:
    - Project memory: architecture, conventions, important decisions, recurring problems
    - Task memory: objective, discoveries, changes, failures per task
    - Agent scratchpad: temporary reasoning/results (auto-cleaned)
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS project_memory (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_memory (
                    task_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, key)
                );

                CREATE TABLE IF NOT EXISTS task_state (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    role TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    parent_task_id TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_scratchpad (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_role TEXT NOT NULL,
                    task_id TEXT,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT,
                    task_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_task_memory_task
                    ON task_memory(task_id);
                CREATE INDEX IF NOT EXISTS idx_task_state_status
                    ON task_state(status);
                CREATE INDEX IF NOT EXISTS idx_event_log_task
                    ON event_log(task_id);
                CREATE INDEX IF NOT EXISTS idx_event_log_timestamp
                    ON event_log(timestamp);
            """)
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # === Project Memory ===

    async def set_project_memory(
        self, key: str, value: str, category: str = "general"
    ) -> None:
        """Store or update a project-level memory."""
        now = datetime.now().isoformat()
        await asyncio.to_thread(self._set_project_memory_sync, key, value, category, now)

    def _set_project_memory_sync(
        self, key: str, value: str, category: str, now: str
    ) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO project_memory
                   (key, value, category, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (key, value, category, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    async def get_project_memory(self, key: str) -> str | None:
        """Get a project-level memory value."""
        return await asyncio.to_thread(self._get_project_memory_sync, key)

    def _get_project_memory_sync(self, key: str) -> str | None:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM project_memory WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    async def get_project_memories(self, category: str | None = None) -> dict[str, str]:
        """Get all project memories, optionally filtered by category."""
        return await asyncio.to_thread(self._get_project_memories_sync, category)

    def _get_project_memories_sync(self, category: str | None) -> dict[str, str]:
        conn = self._get_conn()
        try:
            if category:
                rows = conn.execute(
                    "SELECT key, value FROM project_memory WHERE category = ?",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, value FROM project_memory"
                ).fetchall()
            return {row["key"]: row["value"] for row in rows}
        finally:
            conn.close()

    # === Task Memory ===

    async def set_task_memory(
        self, task_id: str, key: str, value: str, category: str = "general"
    ) -> None:
        """Store a task-level memory."""
        now = datetime.now().isoformat()
        await asyncio.to_thread(
            self._set_task_memory_sync, task_id, key, value, category, now
        )

    def _set_task_memory_sync(
        self, task_id: str, key: str, value: str, category: str, now: str
    ) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO task_memory
                   (task_id, key, value, category, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (task_id, key, value, category, now),
            )
            conn.commit()
        finally:
            conn.close()

    async def get_task_memories(self, task_id: str) -> dict[str, str]:
        """Get all memories for a task."""
        return await asyncio.to_thread(self._get_task_memories_sync, task_id)

    def _get_task_memories_sync(self, task_id: str) -> dict[str, str]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT key, value FROM task_memory WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            return {row["key"]: row["value"] for row in rows}
        finally:
            conn.close()

    # === Task State ===

    async def save_task_state(
        self,
        task_id: str,
        status: str,
        role: str,
        objective: str,
        result_json: str | None = None,
        parent_task_id: str | None = None,
    ) -> None:
        """Save or update task state."""
        now = datetime.now().isoformat()
        await asyncio.to_thread(
            self._save_task_state_sync,
            task_id, status, role, objective, result_json, parent_task_id, now,
        )

    def _save_task_state_sync(
        self,
        task_id: str, status: str, role: str, objective: str,
        result_json: str | None, parent_task_id: str | None, now: str,
    ) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO task_state
                   (task_id, status, role, objective, result_json,
                    created_at, updated_at, parent_task_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, status, role, objective, result_json, now, now, parent_task_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def get_task_state(self, task_id: str) -> dict[str, Any] | None:
        """Get task state."""
        return await asyncio.to_thread(self._get_task_state_sync, task_id)

    def _get_task_state_sync(self, task_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM task_state WHERE task_id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_active_tasks(self) -> list[dict[str, Any]]:
        """Get all active (non-terminal) tasks."""
        return await asyncio.to_thread(self._get_active_tasks_sync)

    def _get_active_tasks_sync(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM task_state
                   WHERE status NOT IN ('completed', 'failed', 'cancelled')
                   ORDER BY created_at"""
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # === Event Log ===

    async def log_event(
        self,
        source: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> None:
        """Log an event."""
        now = datetime.now().isoformat()
        data_json = json.dumps(data) if data else None
        await asyncio.to_thread(
            self._log_event_sync, now, source, event_type, message, data_json, task_id
        )

    def _log_event_sync(
        self, timestamp: str, source: str, event_type: str,
        message: str, data_json: str | None, task_id: str | None,
    ) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO event_log
                   (timestamp, source, event_type, message, data_json, task_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (timestamp, source, event_type, message, data_json, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def get_recent_events(
        self, limit: int = 50, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get recent events."""
        return await asyncio.to_thread(self._get_recent_events_sync, limit, task_id)

    def _get_recent_events_sync(
        self, limit: int, task_id: str | None
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            if task_id:
                rows = conn.execute(
                    """SELECT * FROM event_log WHERE task_id = ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM event_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # === Scratchpad ===

    async def write_scratchpad(
        self, agent_role: str, content: str, task_id: str | None = None
    ) -> None:
        """Write to agent scratchpad (temporary storage)."""
        now = datetime.now().isoformat()
        await asyncio.to_thread(
            self._write_scratchpad_sync, agent_role, task_id, content, now
        )

    def _write_scratchpad_sync(
        self, agent_role: str, task_id: str | None, content: str, now: str
    ) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO agent_scratchpad
                   (agent_role, task_id, content, created_at)
                   VALUES (?, ?, ?, ?)""",
                (agent_role, task_id, content, now),
            )
            conn.commit()
        finally:
            conn.close()

    async def cleanup_scratchpad(self, max_age_hours: int = 24) -> int:
        """Clean up old scratchpad entries. Returns count deleted."""
        return await asyncio.to_thread(self._cleanup_scratchpad_sync, max_age_hours)

    def _cleanup_scratchpad_sync(self, max_age_hours: int) -> int:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                """DELETE FROM agent_scratchpad
                   WHERE created_at < datetime('now', ?)""",
                (f"-{max_age_hours} hours",),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

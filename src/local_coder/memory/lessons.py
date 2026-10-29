"""Explicitly user-authored project lessons injected into future agent tasks."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from local_coder.file_lock import exclusive_lock
from local_coder.workspace import Workspace

MAX_LESSONS = 50
MAX_LESSON_CHARS = 1000
MAX_RENDER_CHARS = 6000


class LessonStoreError(ValueError):
    """The project's lesson file is invalid or a lesson operation is unsafe."""


class ProjectLessonStore:
    """A small local lesson store; only explicit operator input is persisted."""

    def __init__(self, project_root: str | os.PathLike[str]):
        workspace = Workspace(project_root)
        self.path = workspace.resolve(".local-coder/lessons.json")
        self.lock_path = workspace.resolve(".local-coder/lessons.lock")

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LessonStoreError(f"Could not read project lessons at {self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("lessons"), list):
            raise LessonStoreError(f"Invalid project lesson file: {self.path}")
        if len(data["lessons"]) > MAX_LESSONS:
            raise LessonStoreError(f"Project lesson limit exceeded in {self.path}")
        for lesson in data["lessons"]:
            if (
                not isinstance(lesson, dict)
                or not isinstance(lesson.get("id"), str)
                or not isinstance(lesson.get("text"), str)
                or len(lesson["text"]) > MAX_LESSON_CHARS
            ):
                raise LessonStoreError(f"Invalid lesson entry in {self.path}")
        return data["lessons"]

    def list(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            with exclusive_lock(lock):
                return list(self._read_unlocked())

    def add(self, text: str) -> dict[str, Any]:
        normalized = " ".join(text.split())
        if not normalized:
            raise LessonStoreError("A lesson cannot be empty.")
        if len(normalized) > MAX_LESSON_CHARS:
            raise LessonStoreError(f"Lessons must be {MAX_LESSON_CHARS} characters or fewer.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            with exclusive_lock(lock):
                lessons = self._read_unlocked()
                existing = next(
                    (lesson for lesson in lessons if lesson["text"].casefold() == normalized.casefold()),
                    None,
                )
                if existing:
                    return existing
                if len(lessons) >= MAX_LESSONS:
                    raise LessonStoreError(f"Project lesson limit reached ({MAX_LESSONS}).")
                lesson = {
                    "id": uuid.uuid4().hex[:8],
                    "text": normalized,
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                lessons.append(lesson)
                self._write_unlocked(lessons)
                return lesson

    def delete(self, lesson_id: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            with exclusive_lock(lock):
                lessons = self._read_unlocked()
                remaining = [lesson for lesson in lessons if lesson["id"] != lesson_id]
                if len(remaining) == len(lessons):
                    return False
                self._write_unlocked(remaining)
                return True

    def render(self) -> str:
        lessons = self.list()
        if not lessons:
            return ""
        rows = []
        used = 0
        for lesson in reversed(lessons):
            row = f"- {lesson['text']}"
            if used + len(row) > MAX_RENDER_CHARS:
                break
            rows.append(row)
            used += len(row)
        return "\n".join(reversed(rows))

    def _write_unlocked(self, lessons: list[dict[str, Any]]) -> None:
        payload = json.dumps({"version": 1, "lessons": lessons}, indent=2) + "\n"
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

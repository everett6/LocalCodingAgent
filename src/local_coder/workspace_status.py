"""Bounded workbench summaries; report bodies remain behind selected job retrieval."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import re
from threading import Lock
from typing import Any


def recovery_explanation(record: dict[str, Any]) -> dict[str, str]:
    notice = record.get("quota_notice") if isinstance(record.get("quota_notice"), dict) else {}
    if record.get("recovery_required") or notice.get("recovery_required"):
        return {"kind": "reconciliation_required", "explanation": "A tool may have changed the workspace before interruption. Inspect its effects and reconcile the pending journal before continuing; automatic replay is blocked."}
    if record.get("checkpoint"):
        return {"kind": "saved_boundary", "explanation": "Resume continues a saved agent boundary after recovery validation. It does not undo files or resume the entire coding workflow; enclosing work and verification may remain."}
    return {"kind": "new_work", "explanation": "No saved boundary is recorded. Starting a task creates new work with prior report context when available; it does not resume interrupted tool execution."}


def selected_recovery(record: dict[str, Any], project_root: str) -> dict[str, str]:
    """Inspect just the selected checkpoint's pending marker, never walk runs."""
    checkpoint = record.get("checkpoint")
    if not isinstance(checkpoint, str):
        return recovery_explanation(record)
    from local_coder.workspace import Workspace
    try:
        workspace = Workspace(project_root)
        path = workspace.resolve(checkpoint)
        match = re.fullmatch(r"checkpoint_([A-Za-z0-9_-]+)_([0-9]+)\.json", path.name)
        if match:
            pending = workspace.resolve(path.parent / f"run_{match[1]}_pending.json")
            if pending.exists():
                if pending.stat().st_size > 64000:
                    raise ValueError("Pending recovery marker exceeds inspection budget")
                data = json.loads(pending.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or type(data.get("iteration")) is not int:
                    raise ValueError("Pending recovery marker is malformed")
                if data["iteration"] > int(match[2]):
                    return recovery_explanation({"recovery_required": True})
    except (OSError, ValueError):
        return {"kind": "reconciliation_required", "explanation": "Saved recovery evidence could not be safely inspected. Review the checkpoint and pending tool journal before continuing."}
    return recovery_explanation(record)


def observed_state(record: dict[str, Any], *, live: bool = False, waiting: bool = False,
                   now: datetime | None = None) -> dict[str, str]:
    """Describe evidence, never infer verification from model prose or completion."""
    status = record.get("status", "")
    if status == "blocked":
        state, reason = "blocked", "The runner recorded a blocker. Review its saved explanation."
    elif status in {"running", "queued", "cancelling"}:
        state, reason = ("executing", "An active task is registered with this engine.") if live else ("incomplete", "The journal says active, but this engine has no active task.")
        if live and waiting:
            state, reason = "waiting", "A human approval request is pending."
        elif live:
            try:
                stamp = datetime.fromisoformat(record.get("last_activity_at") or record.get("updated_at", ""))
                if stamp.tzinfo is None:
                    stamp = stamp.astimezone()
                if ((now or datetime.now(UTC)) - stamp).total_seconds() > 120:
                    state, reason = "stalled", "No activity observed for over two minutes. The task may still be executing; inspect activity before stopping it."
            except (ValueError, TypeError):
                pass
    elif status == "completed":
        proof = record.get("verification") if isinstance(record.get("verification"), dict) else {}
        verified = proof.get("passed") is True and bool(proof.get("command")) and bool(proof.get("recorded_at"))
        state = "verified" if verified else "completed_unverified"
        reason = "A passing verification command was recorded." if verified else "Execution completed. No structured passing verification evidence is recorded."
    else:
        state, reason = "incomplete", "Work is unfinished or interrupted; review the saved result and recovery guidance."
    return {"observed_state": state, "state_explanation": reason}


class SessionSummaryCache:
    """Refresh only when the atomic session journal changes; retain no report bodies."""

    def __init__(self) -> None:
        self._signature: tuple[int, int, int] | None = None
        self._items: list[dict[str, Any]] = []
        self._lock = Lock()
        self.truncated = False

    def list(self, store: Any, *, limit: int = 200) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Summary limit must be between 1 and 200")
        fields = {"session_id", "request", "status", "phase", "mode", "checkpoint", "engagement_id", "goal_id", "updated_at", "quota_notice", "verification", "recovery_required"}
        with self._lock:
            try:
                stat = store.path.stat()
                signature = stat.st_ino, stat.st_mtime_ns, stat.st_size
            except FileNotFoundError:
                signature = (0, 0, 0)
            if signature != self._signature:
                self._items = []
                records = store.list()
                self.truncated = len(records) > 200
                for record in records[:200]:
                    summary = {key: value for key, value in record.items() if key in fields}
                    summary["request"] = str(summary.get("request", ""))[:2000]
                    summary["has_result"] = bool(record.get("result"))
                    summary["has_error"] = bool(record.get("error"))
                    self._items.append(summary)
                self._signature = signature
            return [dict(item) for item in self._items[:limit]]

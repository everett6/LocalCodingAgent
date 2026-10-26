"""Private immutable proposals, measured validation, activation and rollback.

No Python source in this module is executed. Injected validators and sandbox runners
are trusted engine integrations, never deserialized from model-authored content.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import math
import os
import re
import stat
import time
import uuid
from pathlib import Path

from local_coder.models.profiles import validate_overlay


MAX_SOURCE_BYTES = 64000
MAX_RECORD_BYTES = 128000
_ID = re.compile(r"^[a-f0-9]{32}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class EvolutionStore:
    """Store content-addressed revisions and private separate lifecycle events.

    The model-facing tool does not expose lifecycle writes directly: validate calls
    a host-injected evaluator and activate requires a host-derived approval.
    """

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve(strict=True)
        self.root = self.project_root / ".local-coder" / "harness"
        self._ensure_tree()

    def _ensure_tree(self):
        for path in (self.project_root / ".local-coder", self.root, self.root / "revisions", self.root / "tools", self.root / "events"):
            if path.is_symlink():
                raise ValueError("Harness storage may not contain symlinks")
            path.mkdir(mode=0o700, exist_ok=True)
            if not path.is_dir() or (os.name != "nt" and path.stat().st_uid != os.getuid()):
                raise ValueError("Harness storage must be an owner-controlled directory")
            # .local-coder may serve other features; only this feature's tree is private.
            if path != self.project_root / ".local-coder":
                path.chmod(0o700)

    def _path(self, relative: str) -> Path:
        self._ensure_tree()
        path = self.root / relative
        for item in (path, *path.parents):
            if item == self.project_root:
                break
            if item.is_symlink():
                raise ValueError("Harness paths may not contain symlinks")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Harness path escaped private storage")
        return path

    def _read(self, relative: str) -> dict:
        path = self._path(relative)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                raise ValueError("Harness record must be a private owner-only regular file")
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw.encode()) > MAX_RECORD_BYTES:
            raise ValueError("Harness record is too large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid harness record")
        return result

    def _write(self, relative: str, data: dict):
        raw = json.dumps(data, sort_keys=True, allow_nan=False)
        if len(raw.encode()) > MAX_RECORD_BYTES:
            raise ValueError("Harness record is too large")
        path = self._path(relative)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(raw)

    @staticmethod
    def _hash(data):
        return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def _candidate(self, revision_id: str) -> dict:
        if not isinstance(revision_id, str) or not _ID.fullmatch(revision_id):
            raise ValueError("Invalid revision ID")
        revision = self._read(f"revisions/{revision_id}.json")
        if revision.get("digest") != self._hash(revision.get("payload")):
            raise ValueError("Revision content changed after proposal")
        return revision

    def get_revision(self, revision_id: str) -> dict:
        """Return the exact immutable candidate for host approval and evaluation."""
        return self._candidate(revision_id)

    def _events(self):
        return sorted((self._read(f"events/{path.name}") for path in self._path("events").glob("*.json")), key=lambda item: (item["time_ns"], item["event_id"]))

    def _event(self, revision_id: str, state: str, **data):
        event_id = uuid.uuid4().hex
        event = {"event_id": event_id, "revision_id": revision_id, "state": state, "time_ns": time.time_ns(), **data}
        self._write(f"events/{event_id}.json", event)
        return event

    def propose(self, kind: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Proposal payload must be an object")
        from local_coder.memory.context_store import _clean
        if _clean(payload) != payload:
            raise ValueError("Remove credential values before persisting a harness proposal")
        payload = copy.deepcopy(payload)
        if kind == "tool":
            if set(payload) != {"name", "source"} or not isinstance(payload["name"], str) or not _NAME.fullmatch(payload["name"]):
                raise ValueError("Tool requires a safe name and Python source only")
            source = payload["source"]
            if not isinstance(source, str) or not source.strip() or len(source.encode()) > MAX_SOURCE_BYTES:
                raise ValueError("Tool source must be nonempty and bounded")
            try:
                ast.parse(source)
            except (SyntaxError, RecursionError) as exc:
                raise ValueError(f"Tool source failed AST validation: {exc}") from exc
        elif kind in {"instructions", "skill"}:
            if set(payload) != {"text"} or not isinstance(payload["text"], str) or not 0 < len(payload["text"]) <= 16000:
                raise ValueError("Instructions/skills require bounded text only")
        elif kind == "loop_plan":
            payload = validate_overlay(payload)
            if "instructions" in payload:
                raise ValueError("Loop plans contain declarative controls only")
        else:
            raise ValueError("Unknown proposal kind")
        revision_id = uuid.uuid4().hex
        revision = {"revision_id": revision_id, "kind": kind, "payload": payload, "digest": self._hash(payload), "created_ns": time.time_ns()}
        self._write(f"revisions/{revision_id}.json", revision)
        if kind == "tool":
            # Source is data, stored in an immutable JSON envelope, never imported.
            self._write(f"tools/{revision_id}.json", {"name": payload["name"], "source": payload["source"], "digest": revision["digest"]})
        self._event(revision_id, "candidate")
        return {**revision, "state": "candidate"}

    async def validate(self, revision_id: str, validator) -> dict:
        revision = self._candidate(revision_id)
        if validator is None or not callable(validator):
            raise ValueError("An injected local evaluator is required for validation")
        result = validator(copy.deepcopy(revision))
        if inspect.isawaitable(result):
            result = await result
        measurements = result.get("measurements") if isinstance(result, dict) else None
        if not isinstance(result, dict) or type(result.get("success")) is not bool or not isinstance(measurements, dict) or not measurements:
            raise ValueError("Evaluator must return success and actual local measurements")
        if any(not isinstance(key, str) or type(value) not in (int, float) or not math.isfinite(value) or value < 0 for key, value in measurements.items()):
            raise ValueError("Measurements must be finite nonnegative numeric observations")
        state = "validated" if result["success"] else "rejected"
        self._event(revision_id, state, digest=revision["digest"], measurements=measurements)
        return {**revision, "state": state, "measurements": measurements}

    def _validated(self, revision_id):
        revision = self._candidate(revision_id)
        evaluations = [e for e in self._events() if e["revision_id"] == revision_id and e["state"] in {"validated", "rejected"}]
        if not evaluations or evaluations[-1]["state"] != "validated" or evaluations[-1].get("digest") != revision["digest"]:
            raise ValueError("Revision requires a passing injected local evaluation")
        return revision

    def activate(self, revision_id: str, *, approved: bool) -> dict:
        if approved is not True:
            raise PermissionError("Revision activation requires engine approval")
        revision = self._validated(revision_id)
        self._event(revision_id, "active", kind=revision["kind"], digest=revision["digest"])
        return {**revision, "state": "active"}

    def rollback(self, revision_id: str, *, approved: bool) -> dict:
        if not any(e["revision_id"] == revision_id and e["state"] == "active" for e in self._events()):
            raise ValueError("Rollback target must be a previously active revision")
        return self.activate(revision_id, approved=approved)

    def record_outcome(self, revision_id: str, outcome: dict) -> dict:
        self._candidate(revision_id)
        if not isinstance(outcome, dict):
            raise ValueError("Outcome must be an object")
        from local_coder.memory.context_store import _clean
        return self._event(revision_id, "observation", outcome=_clean(outcome), trusted_instructions=False)

    def active_revisions(self) -> list[dict]:
        active = {}
        for event in self._events():
            if event["state"] == "active":
                revision = self._validated(event["revision_id"])
                key = (revision["kind"], revision["payload"].get("name") if revision["kind"] == "tool" else "")
                active[key] = {**revision, "state": "active"}
        return list(active.values())

    def active_overlay(self) -> dict:
        overlay = {}
        for revision in self.active_revisions():
            if revision["kind"] == "instructions":
                overlay["instructions"] = revision["payload"]["text"]
            elif revision["kind"] == "loop_plan":
                overlay.update(revision["payload"])
        return overlay

    def history(self) -> list[dict]:
        events = self._events()
        result = []
        for path in self._path("revisions").glob("*.json"):
            revision = self._candidate(path.stem)
            lifecycle = [e for e in events if e["revision_id"] == path.stem]
            transitions = [event for event in lifecycle if event["state"] != "observation"]
            result.append({**revision, "state": transitions[-1]["state"] if transitions else "candidate", "events": lifecycle})
        return sorted(result, key=lambda r: r["created_ns"])

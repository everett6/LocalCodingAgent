"""Host-owned finding lifecycle and revision-bound verification evidence."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from local_coder.memory.context_store import ContextMemory, _clean
from local_coder.memory.goals import GoalLedger
from local_coder.security.ledger import FindingsLedger
from local_coder.workspace import Workspace


class FindingReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    verdict: Literal["supported", "disputed", "needs_evidence"]
    input_trace: str = Field(min_length=1, max_length=4000)
    prerequisites: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    counterevidence: str = Field(min_length=1, max_length=4000)


class EvidenceStore:
    def __init__(self, root):
        self.memory = ContextMemory(root)
        self.root = self.memory.project_root
        with self.memory._connection() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS security_evidence
                (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS security_events
                (seq INTEGER PRIMARY KEY AUTOINCREMENT, finding_id TEXT NOT NULL, payload TEXT NOT NULL);""")

    def _write(self, db, row, event):
        row = _clean(row)
        row.update(self._proof_disposition(row))
        db.execute("INSERT INTO security_evidence VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                   (row["id"], json.dumps(row)))
        db.execute("INSERT INTO security_events(finding_id,payload) VALUES (?,?)", (row["id"], json.dumps({
            "event": event, "time": datetime.now(timezone.utc).isoformat(), "state": row})))
        return row

    def sync(self):
        """Import scanner/reviewer claims without treating them as verification."""
        ledger = FindingsLedger.for_project(self.root)
        with self.memory._connection() as db:
            for entry in ledger.entries:
                identity = hashlib.sha256(entry.key.encode()).hexdigest()[:24]
                # Bind legacy rows once. The ledger ID survives an explicit
                # location update; path/rule alone can no longer merge occurrences.
                legacy = hashlib.sha256(entry.legacy_key.encode()).hexdigest()[:24]
                for saved in db.execute("SELECT payload FROM security_evidence"):
                    candidate = json.loads(saved[0])
                    observed = candidate.get("finding", {})
                    if candidate.get("ledger_id") == entry.id or (
                        candidate["id"] == legacy and not candidate.get("ledger_id")
                        and (not observed or (observed.get("path") == entry.path
                                             and observed.get("line", 0) == entry.line))):
                        identity = candidate["id"]
                        break
                source = {name: getattr(entry, name) for name in
                          ("title", "path", "line", "rule", "severity", "exploit_path", "fix")}
                old = db.execute("SELECT payload FROM security_evidence WHERE id=?", (identity,)).fetchone()
                if old:
                    row = json.loads(old[0])
                    if (row["finding"] == _clean(source) and row.get("ledger_id") == entry.id
                            and (entry.status != "false_positive" or row["suppression"])):
                        continue
                    row.update(finding=source, state="suppressed" if row["suppression"] else "candidate", review=None, before=None, after=None)
                else:
                    row = {"id": identity, "finding": source, "state": "candidate", "review": None,
                           "before": None, "after": None, "suppression": ""}
                row["ledger_id"] = entry.id
                if entry.status == "false_positive":
                    row.update(state="suppressed", suppression=row["suppression"] or entry.lesson or
                               "Imported ledger false-positive decision")
                self._write(db, row, "discovered" if not old else "finding_changed")

    @staticmethod
    def _get(db, identity):
        found = db.execute("SELECT payload FROM security_evidence WHERE id=?", (identity,)).fetchone()
        if not found:
            raise ValueError("Unknown finding; run a local security review first")
        return json.loads(found[0])

    def get(self, identity):
        with self.memory._connection() as db:
            return self._get(db, identity)

    def list(self):
        with self.memory._connection() as db:
            rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM security_evidence ORDER BY rowid")]
        try:
            revision = GoalLedger(self.root).revision()
        except ValueError:
            revision = None
        for row in rows:
            row.update(self._proof_disposition(row))
            proof = row.get("after") or row.get("before") or row.get("review")
            row["evidence_stale"] = bool(proof) and proof["revision"] != revision
            if row["evidence_stale"]:
                row.update(strict_verified=False, strict_reproduced=False,
                           verification_disposition="stale_evidence")
        return rows

    def review(self, identity, review: FindingReview, *, reviewer_task: str, revision: str, finding: dict):
        with self.memory._connection() as db:
            row = self._get(db, identity)
            if row["finding"] != finding:
                raise ValueError("Finding changed during review; review it again")
            if row["suppression"]:
                raise ValueError("Finding is suppressed; explicitly reopen it first")
            row.update(state="reviewed" if review.verdict == "supported" else "candidate", before=None, after=None,
                       review={**review.model_dump(), "reviewer_task": reviewer_task,
                               "revision": revision, "role": "reviewer"})
            return self._write(db, row, "independent_review")

    def suppress(self, identity, reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ValueError("A bounded suppression reason is required")
        with self.memory._connection() as db:
            row = self._get(db, identity)
            row.update(state="suppressed", suppression=reason.strip())
            return self._write(db, row, "human_suppression")

    def reopen(self, identity):
        with self.memory._connection() as db:
            row = self._get(db, identity)
            row.update(state="candidate", suppression="", review=None, before=None, after=None)
            return self._write(db, row, "human_reopened")

    @staticmethod
    def _proof_disposition(row):
        before, after = row.get("before"), row.get("after")
        control = (after or before or {}).get("negative_controls", {"status": "missing", "test_ids": []})
        stage = "scanner_candidate"
        if row.get("review"):
            stage = "model_assessment"
        if before:
            stage = "reproduction_observation"
        verified = bool(not row.get("suppression") and before and after and control["status"] == "passed"
                        and before.get("negative_controls", {}).get("status") == "passed")
        if verified:
            stage = "verified_fix"
        return {"proof_stage": stage, "negative_controls": control,
                "strict_reproduced": bool(not row.get("suppression") and before and before.get("negative_controls", {}).get("status") == "passed"),
                "strict_verified": verified,
                "verification_disposition": "verified" if verified else
                "unverified_missing_controls" if before and control["status"] != "passed" else "unverified"}

    def export_package(self, identity):
        row = self.get(identity)
        try:
            revision = GoalLedger(self.root).revision()
        except ValueError:
            revision = None
        review = row.get("review") or {}
        disposition = self._proof_disposition(row)
        proof = row.get("after") or row.get("before") or row.get("review")
        if proof and proof["revision"] != revision:
            disposition.update(strict_verified=False, strict_reproduced=False,
                               verification_disposition="stale_evidence")
        return {"finding_id": identity, "revision": revision,
                "prerequisites": review.get("prerequisites", "unknown"),
                "evidence": {"finding": row["finding"], "review": row.get("review")},
                "reproduction": row.get("before"), "verification": row.get("after"),
                "impact": review.get("impact", "unverified candidate"),
                "remediation": row["finding"].get("fix") or "not recorded",
                "suppression": row["suppression"], **disposition}

    async def verify_controlled(self, identity, *, negative_control_ids, **kwargs):
        """Strict path: only observed passing control IDs can support proof."""
        return await self.verify(identity, negative_control_ids=negative_control_ids,
                                 require_controls=True, **kwargs)

    async def verify(self, identity, *, phase, test_path, image, backend="docker", timeout_seconds=60,
                     negative_control_ids=None, require_controls=False):
        from local_coder.security.regression import run_regression
        if phase not in {"before", "after"}:
            raise ValueError("Phase must be before or after")
        controls = [] if negative_control_ids is None else negative_control_ids
        if (not isinstance(controls, list) or len(controls) > 20
                or any(not isinstance(item, str) or not 1 <= len(item) <= 1024 for item in controls)
                or len(set(controls)) != len(controls)):
            raise ValueError("Use at most 20 distinct bounded negative-control test IDs")
        if require_controls and not controls:
            raise ValueError("Observed negative controls are required for strict proof")
        row = self.get(identity)
        if row["suppression"] or not row["review"] or row["review"]["verdict"] != "supported":
            raise ValueError("An independent supported review is required before reproduction")
        path = Workspace(str(self.root)).resolve(test_path)
        relative = path.relative_to(self.root)
        if not relative.parts or relative.parts[0] != "tests" or path.suffix != ".py" or not path.is_file():
            raise ValueError("Regression test must be a Python file under tests/")
        if path.stat().st_size > 262144:
            raise ValueError("Regression test is too large")
        test_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        revision = await asyncio.to_thread(GoalLedger(self.root).revision)
        if phase == "before" and row["review"]["revision"] != revision:
            raise ValueError("Review evidence is stale; review the current source before reproduction")
        if phase == "after" and not row["before"]:
            raise ValueError("Record a failing before-fix regression first")
        result = await run_regression(self.root, relative.as_posix(), image, timeout_seconds=timeout_seconds, backend=backend)
        result = _clean(result)
        end_revision = await asyncio.to_thread(GoalLedger(self.root).revision)
        if end_revision != revision or hashlib.sha256(path.read_bytes()).hexdigest() != test_digest:
            raise ValueError("Workspace changed during verification; result was not accepted")
        controls_passed = bool(controls) and set(controls) <= set(result.get("passed_test_ids", []))
        control_evidence = {"status": "passed" if controls_passed else "failed" if controls else "missing",
                            "test_ids": list(controls)}
        proof = {"revision": revision, "test_path": relative.as_posix(), "test_digest": test_digest,
                 **result, "negative_controls": control_evidence}
        if require_controls and not controls_passed:
            with self.memory._connection() as db:
                db.execute("INSERT INTO security_events(finding_id,payload) VALUES (?,?)",
                           (identity, json.dumps({"event": "controls_insufficient", "evidence": proof})))
            raise ValueError("Negative controls did not pass; reproduction/fix proof was not accepted")
        with self.memory._connection() as db:
            current = self._get(db, identity)
            if current != row:
                raise ValueError("Finding changed during verification; rerun it")
            status = result["status"]
            if phase == "after" and status in {"passed", "failed"}:
                baseline = row["before"]
                for field in ("test_path", "test_digest", "test_ids", "image_id", "backend", "negative_controls"):
                    if not proof.get(field) or proof[field] != baseline.get(field):
                        raise ValueError(f"Before/after {field} must match")
            if phase == "before" and status == "failed" and result.get("failed", 0) > 0 and result.get("failure_kind") == "assertion":
                row.update(state="reproduced", before=proof, after=None)
            elif phase == "after" and status == "passed":
                baseline = row["before"]
                if revision == baseline["revision"]:
                    raise ValueError("A changed source revision is required to verify a fix")
                row.update(state="reverified" if row["after"] else "fixed", after=proof)
            elif phase == "after" and status == "failed":
                row.update(state="reopened", after=None)
            elif phase == "before":
                row.update(state="reviewed", before=None, after=None)
            self._write(db, row, f"verification_{phase}_{status}")
            # Keep failed infrastructure attempts auditable without promoting a lifecycle state.
            db.execute("INSERT INTO security_events(finding_id,payload) VALUES (?,?)",
                       (identity, json.dumps(_clean({"event": "verification_result", "evidence": proof}))))
        return {"finding_id": identity, "state": row["state"], "result": result,
                **self._proof_disposition(row)}

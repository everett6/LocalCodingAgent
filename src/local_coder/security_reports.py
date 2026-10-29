"""Bounded security report interchange and a private SHA-256 evidence chain.

Hashes establish consistency with an independently retained head, not signatures,
authorship, or proof that a reported test ran. Without an external head, deleting
the end of a chain or replacing the whole chain cannot be detected.

SARIF subset follows OASIS SARIF 2.1.0 sections 3.13, 3.19, 3.27 and 3.30:
https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html
External properties and non-repository artifact URIs are intentionally rejected.
"""

# HTTP handlers consistently translate validation ValueError into a bad request.
# ruff: noqa: TRY004

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote

from local_coder.file_lock import exclusive_lock
from local_coder.memory.context_store import _clean
from local_coder.statedir import state_path

MAX_DOCUMENT_BYTES = 4_000_000
MAX_LEDGER_BYTES = 8_000_000
MAX_RECORD_BYTES = 320_000
MAX_FINDINGS = 10_000
MAX_RECORDS = 10_000
LOCK_TIMEOUT_SECONDS = 3.0
EVIDENCE_PATH = ".local-coder/security/evidence.jsonl"
_ZERO = "0" * 64
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

# Exact names only: generic 'injection' and 'authorization' are ambiguous.
# Mapping definitions: https://cwe.mitre.org/data/definitions/<number>.html
_CWE = {
    "sql_injection": "CWE-89",
    "os_command_injection": "CWE-78",
    "command_injection": "CWE-78",
    "cross_site_scripting": "CWE-79",
    "xss": "CWE-79",
    "path_traversal": "CWE-22",
    "server_side_request_forgery": "CWE-918",
    "ssrf": "CWE-918",
    "insecure_deserialization": "CWE-502",
}


def _canonical(value) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError(
            "Report data must contain finite bounded JSON values"
        ) from None


def _bounded(value, maximum=MAX_DOCUMENT_BYTES):
    """Validate before redaction or serialization, avoiding recursive explosions."""
    stack = [(value, 0)]
    nodes = 0
    size = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 16 or nodes > 150_000:
            raise ValueError("Report structure exceeds limits")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Report object keys must be strings")
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            size += len(item)
        elif (
            item is None
            or type(item) in (bool, int)
            or type(item) is float
            and math.isfinite(item)
        ):
            pass
        else:
            raise ValueError("Report data must contain finite JSON values")
        if size > maximum:
            raise ValueError("Report exceeds size limit")
    encoded = _canonical(value)
    if len(encoded) > maximum:
        raise ValueError("Report exceeds size limit")
    return value


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _parse(data, maximum):
    if isinstance(data, (str, bytes)):
        if len(data) > maximum:
            raise ValueError("Report exceeds size limit")
        try:
            data = json.loads(data, object_pairs_hook=_no_duplicates)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid report JSON") from None
    return _bounded(data, maximum)


def _redacted(data):
    """Also remove copies of explicitly named secret values in other fields."""
    secrets = set()
    pending = [(data, False)]
    while pending:
        value, sensitive = pending.pop()
        if isinstance(value, dict):
            pending.extend(
                (
                    item,
                    sensitive
                    or bool(
                        re.search(
                            r"password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key",
                            key,
                            re.IGNORECASE,
                        )
                    ),
                )
                for key, item in value.items()
            )
        elif isinstance(value, list):
            pending.extend((item, sensitive) for item in value)
        elif sensitive and isinstance(value, str) and len(value) >= 4:
            secrets.add(value)
            if len(secrets) > 256:
                raise ValueError("Too many credential values in report data")

    secrets = sorted(secrets, key=len, reverse=True)

    def scrub(value):
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {scrub(key): scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return scrub(_clean(data))


def _text(value, maximum=8192, default=""):
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Report text exceeds limits")
    # Keep line breaks and tabs; exclude terminal controls and bidi overrides.
    return _clean(
        "".join(
            char
            for char in value
            if (ord(char) >= 32 or char in "\n\t")
            and not 0x202A <= ord(char) <= 0x202E
            and not 0x2066 <= ord(char) <= 0x2069
        )
    )


def _path(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid repository artifact path")
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    while value.startswith("./"):
        value = value[2:]
    if (
        not value
        or value.startswith("/")
        or any(char in value for char in "\\:%?#")
        or any(ord(char) < 32 for char in value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("Artifact paths must be relative repository paths")
    return _text(value, 2048)


def _cwe(row):
    supplied = row.get("cwe", row.get("CWE"))
    if supplied:
        if not isinstance(supplied, str) or not re.fullmatch(
            r"CWE-[1-9][0-9]{0,4}", supplied
        ):
            raise ValueError("Invalid CWE identifier")
        return supplied, "supplied"
    for key in ("type", "category", "rule"):
        name = row.get(key, "")
        if isinstance(name, str):
            normalized = name.lower().replace("-", "_").replace(" ", "_")
            if normalized in _CWE:
                return _CWE[normalized], "exact_type_mapping"
    return None, None


def _source_metadata(raw, evidence):
    metadata = {}
    for key in ("source_sha256", "audit_record_hash"):
        value = raw.get(key, evidence.get(key))
        if value is not None and (
            not isinstance(value, str) or not _HASH.fullmatch(value)
        ):
            raise ValueError(
                "Source and audit record fingerprints must be SHA-256 digests"
            )
        metadata[key] = value
    recorded_at = raw.get("audit_recorded_at")
    if recorded_at is not None:
        recorded_at = _text(recorded_at, 80)
        try:
            if datetime.fromisoformat(recorded_at).tzinfo is None:
                raise ValueError("Missing timezone")
        except ValueError:
            raise ValueError(
                "Audit record timestamps must use timezone-aware ISO format"
            ) from None
    metadata["audit_recorded_at"] = recorded_at
    status = raw.get(
        "source_status", "unavailable" if metadata["source_sha256"] else "not_recorded"
    )
    if not isinstance(status, str) or status not in {
        "unchanged",
        "changed",
        "missing",
        "unavailable",
        "not_recorded",
    }:
        raise ValueError("Invalid current source status")
    if status == "unchanged" and not metadata["source_sha256"]:
        raise ValueError("An unchanged source status requires a recorded source hash")
    metadata["source_status"] = status
    metadata["source_qualification"] = {
        "unchanged": "Current source content matches the recorded hash; this does not verify the finding.",
        "changed": "Historical observation: source content changed after this record.",
        "missing": "Historical observation: the recorded source is now missing.",
        "unavailable": "Historical observation: current source content could not be checked.",
        "not_recorded": "Historical observation: no source content hash was recorded.",
    }[status]
    return metadata


def _findings(findings):
    _bounded(findings)
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise ValueError("Findings must be a bounded list")
    result = []
    for raw in findings:
        if not isinstance(raw, dict):
            raise ValueError("Each finding must be an object")
        raw = _redacted(raw)
        evidence = raw.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        raw.setdefault("rule", raw.get("rule_id", ""))
        raw.setdefault("path", raw.get("file", evidence.get("file", "")))
        raw.setdefault("line", evidence.get("line", 0))
        raw.setdefault("fix", raw.get("remediation", ""))
        if not raw.get("description") and isinstance(raw.get("reasons"), list):
            raw["description"] = "\n".join(
                _text(reason, 2048) for reason in raw["reasons"]
            )
        if not raw.get("exploit_path") and evidence.get("source_trace"):
            trace = evidence["source_trace"]
            raw["exploit_path"] = (
                trace if isinstance(trace, str) else _canonical(trace).decode("utf-8")
            )
        row = {
            key: _text(raw.get(key), maximum)
            for key, maximum in (
                ("id", 200),
                ("title", 500),
                ("description", 8192),
                ("rule", 200),
                ("exploit_path", 8192),
                ("fix", 8192),
                ("status", 80),
                ("verification", 80),
            )
        }
        row["title"] = (
            row["title"]
            or _text(raw.get("message"), 500)
            or row["rule"]
            or "Security finding"
        )
        row["description"] = row["description"] or _text(raw.get("message"), 8192)
        row["rule"] = row["rule"] or "security-finding"
        row["path"] = (
            _path(raw.get("path", raw.get("file")))
            if raw.get("path", raw.get("file"))
            else ""
        )
        line = raw.get("line", 0)
        if type(line) is not int or not 0 <= line <= 1_000_000_000:
            raise ValueError("Finding line must be a nonnegative integer")
        row["line"] = line
        severity = _text(raw.get("severity", "MEDIUM"), 20).upper()
        if severity not in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}:
            raise ValueError("Invalid finding severity")
        row["severity"] = severity
        row["verification"] = row["verification"] or "unverified"
        row["status"] = row["status"] or "suspected"
        row["cwe"], row["cwe_source"] = _cwe(raw)
        row.update(_source_metadata(raw, evidence))
        row["suppression"] = _text(raw.get("suppression"), 2000)
        package = raw.get("package", {})
        if not isinstance(package, dict):
            raise ValueError("Finding package must be an object")
        package = dict(package)
        defaults = {"revision": None, "prerequisites": "unknown", "evidence": evidence,
                    "reproduction": None, "impact": "unverified candidate",
                    "remediation": row["fix"] or "not recorded", "verification_disposition": "unverified"}
        for key, value in defaults.items():
            package.setdefault(key, value)
        row["package"] = _bounded(package, MAX_RECORD_BYTES)
        result.append(row)
    return result


class EvidenceStore:
    """Append-only API over an atomically replaced, hash-chained local journal."""

    def __init__(self, project_root):
        self.project_root = Path(project_root).resolve()
        self.path = self.project_root / EVIDENCE_PATH
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    def _safe_path(self, relative):
        target = state_path(self.project_root, relative)
        for directory in (target.parent.parent, target.parent):
            if os.name != "nt":
                directory.chmod(0o700)
        if target.exists():
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Evidence storage must be an unlinked regular file")
        return target

    @contextmanager
    def _local_lock(self):
        if not self._lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            raise ValueError("Evidence storage is busy; retry later")
        try:
            yield
        finally:
            self._lock.release()

    @contextmanager
    def _locked(self):
        with self._local_lock():
            target = self._safe_path(EVIDENCE_PATH + ".lock")
            descriptor = os.open(
                target, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            with os.fdopen(descriptor, "r+b") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Invalid evidence lock file")
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                with ExitStack() as held:
                    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
                    while True:
                        try:
                            held.enter_context(exclusive_lock(stream, blocking=False))
                            break
                        except (BlockingIOError, PermissionError):
                            if time.monotonic() >= deadline:
                                raise ValueError(
                                    "Evidence storage is busy; retry later"
                                ) from None
                            time.sleep(0.025)
                    yield

    def _load(self):
        target = self._safe_path(EVIDENCE_PATH)
        if not target.exists():
            return []
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_LEDGER_BYTES
            ):
                raise ValueError("Invalid or oversized evidence storage")
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            data = stream.read(MAX_LEDGER_BYTES + 1)
        if len(data) > MAX_LEDGER_BYTES or (data and not data.endswith(b"\n")):
            raise ValueError("Invalid or oversized evidence storage")
        lines = data.splitlines()
        if len(lines) > MAX_RECORDS:
            raise ValueError("Evidence record limit exceeded")
        rows, previous = [], _ZERO
        for index, line in enumerate(lines, 1):
            row = _parse(line, MAX_RECORD_BYTES)
            if (
                not isinstance(row, dict)
                or set(row)
                != {
                    "version",
                    "index",
                    "created_at",
                    "kind",
                    "data",
                    "previous_hash",
                    "hash",
                }
                or type(row["version"]) is not int
                or row["version"] != 1
                or type(row["index"]) is not int
                or row["index"] != index
                or row["previous_hash"] != previous
                or not isinstance(row["hash"], str)
                or not _HASH.fullmatch(row["hash"])
                or not isinstance(row["kind"], str)
                or not _KIND.fullmatch(row["kind"])
                or not isinstance(row["data"], dict)
                or not isinstance(row["created_at"], str)
            ):
                raise ValueError("Evidence chain structure is invalid")
            unsigned = {key: value for key, value in row.items() if key != "hash"}
            if hashlib.sha256(_canonical(unsigned)).hexdigest() != row["hash"]:
                raise ValueError("Evidence hash mismatch")
            rows.append(row)
            previous = row["hash"]
        return rows

    def read(self, limit=1000):
        if type(limit) is not int or not 1 <= limit <= MAX_RECORDS:
            raise ValueError("Invalid evidence read limit")
        with self._locked():
            return _clean(self._load()[-limit:])

    def verify(self, expected_head=None):
        if expected_head is not None and (
            not isinstance(expected_head, str) or not _HASH.fullmatch(expected_head)
        ):
            raise ValueError("Expected head must be a SHA-256 digest")
        try:
            with self._locked():
                rows = self._load()
                head = rows[-1]["hash"] if rows else _ZERO
                valid = expected_head is None or expected_head == head
                result = {
                    "valid": valid,
                    "records": len(rows),
                    "head_hash": head,
                    "anchored": expected_head is not None,
                    "algorithm": "sha256",
                }
                if not valid:
                    result["error"] = "Evidence head differs from the supplied anchor"
                return result
        except (ValueError, OSError):
            return {
                "valid": False,
                "records": 0,
                "head_hash": None,
                "anchored": expected_head is not None,
                "algorithm": "sha256",
                "error": "Evidence chain is invalid or unavailable",
            }

    def append(self, kind, data):
        if (
            not isinstance(kind, str)
            or not _KIND.fullmatch(kind)
            or not isinstance(data, dict)
        ):
            raise ValueError("Evidence requires a bounded kind and JSON object")
        _bounded(data, MAX_RECORD_BYTES - 1024)
        cleaned = _redacted(data)
        with self._locked():
            rows = self._load()
            if kind == "remediation_verification":
                cleaned = self._remediation(rows, cleaned)
            return self._append(rows, kind, cleaned)

    def _append(self, rows, kind, data):
        if len(rows) >= MAX_RECORDS:
            raise ValueError("Evidence record limit exceeded")
        record = {
            "version": 1,
            "index": len(rows) + 1,
            "created_at": datetime.now(UTC).isoformat(),
            "kind": kind,
            "data": data,
            "previous_hash": rows[-1]["hash"] if rows else _ZERO,
        }
        record["hash"] = hashlib.sha256(_canonical(record)).hexdigest()
        _bounded(record, MAX_RECORD_BYTES)
        content = b"".join(_canonical(row) + b"\n" for row in [*rows, record])
        if len(content) > MAX_LEDGER_BYTES:
            raise ValueError("Evidence storage limit exceeded")
        target = self._safe_path(EVIDENCE_PATH)
        descriptor, temporary = tempfile.mkstemp(prefix=".evidence-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._safe_path(EVIDENCE_PATH)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return record

    @staticmethod
    def _remediation(rows, data):
        lookup = {row["hash"]: row for row in rows}
        if any(
            not isinstance(data.get(key), str) or not _HASH.fullmatch(data[key])
            for key in ("before_hash", "after_hash")
        ):
            raise ValueError("Remediation requires SHA-256 evidence references")
        before = lookup.get(data.get("before_hash"))
        after = lookup.get(data.get("after_hash"))
        if not before or not after or before["index"] >= after["index"]:
            raise ValueError(
                "Remediation requires ordered, existing evidence references"
            )
        first, last = before["data"], after["data"]
        case = first.get("case_hash")
        finding = data.get("finding_id")
        if (
            before["kind"] != "verification_attempt"
            or after["kind"] != "verification_attempt"
            or not isinstance(case, str)
            or not _HASH.fullmatch(case)
            or not isinstance(finding, str)
            or not finding
            or len(finding) > 200
            or first.get("finding_id") != finding
            or last.get("finding_id") != finding
            or last.get("case_hash") != case
            or first.get("outcome") != "triggered"
            or last.get("outcome") != "not_triggered"
        ):
            raise ValueError(
                "Remediation requires the same finding and case, triggered before and not triggered after"
            )
        return {
            "finding_id": finding,
            "case_hash": case,
            "before_hash": before["hash"],
            "after_hash": after["hash"],
            "verification": "reported",
        }

    def record_remediation(self, finding_id, before_hash, after_hash):
        return self.append(
            "remediation_verification",
            {
                "finding_id": finding_id,
                "before_hash": before_hash,
                "after_hash": after_hash,
            },
        )


def export_sarif(findings: list[dict]) -> dict:
    """Export observations without manufacturing CVSS scores or verification."""
    rows = _findings(findings)
    rules, indices, results = [], {}, []
    levels = {
        "CRITICAL": "error",
        "HIGH": "error",
        "MEDIUM": "warning",
        "LOW": "note",
        "INFO": "note",
    }
    for row in rows:
        rule_id = row["rule"]
        if rule_id not in indices:
            indices[rule_id] = len(rules)
            rules.append(
                {
                    "id": rule_id,
                    "shortDescription": {"text": row["title"]},
                    "properties": {"tags": []},
                }
            )
        if (
            row["cwe"]
            and row["cwe"] not in rules[indices[rule_id]]["properties"]["tags"]
        ):
            rules[indices[rule_id]]["properties"]["tags"].append(row["cwe"])
        result = {
            "ruleId": rule_id,
            "ruleIndex": indices[rule_id],
            "level": levels[row["severity"]],
            "message": {"text": row["description"] or row["title"]},
            "properties": {
                key: row[key]
                for key in (
                    "id",
                    "title",
                    "severity",
                    "status",
                    "verification",
                    "cwe",
                    "cwe_source",
                    "exploit_path",
                    "fix",
                    "source_sha256",
                    "audit_record_hash",
                    "audit_recorded_at",
                    "source_status",
                    "source_qualification",
                    "suppression",
                    "package",
                )
            },
        }
        if row["status"] in {"false_positive", "suppressed"} or row["suppression"]:
            result["suppressions"] = [{"kind": "external", "status": "accepted",
                                       "justification": row["suppression"] or "Recorded false-positive decision"}]
        if row["path"]:
            physical = {"artifactLocation": {"uri": quote(row["path"], safe="/")}}
            if row["line"]:
                physical["region"] = {"startLine": row["line"]}
            result["locations"] = [{"physicalLocation": physical}]
        results.append(result)
    document = {
        "version": "2.1.0",
        "$schema": "https://docs.oasis-open.org/sarif/sarif/v2.1.0/cos02/schemas/sarif-schema-2.1.0.json",
        "runs": [
            {
                "tool": {"driver": {"name": "local-coder", "rules": rules}},
                "results": results,
            }
        ],
    }
    return _bounded(document)


def import_sarif(data) -> list[dict]:
    """Import a self-contained SARIF subset as unverified local findings."""
    document = _parse(data, MAX_DOCUMENT_BYTES)
    if not isinstance(document, dict) or document.get("version") != "2.1.0":
        raise ValueError("Expected SARIF 2.1.0")
    runs = document.get("runs")
    if not isinstance(runs, list) or len(runs) > 100:
        raise ValueError("SARIF runs must be a bounded list")
    imported = []
    for run in runs:
        if not isinstance(run, dict) or run.get("externalPropertyFileReferences"):
            raise ValueError("External SARIF properties are not supported")
        tool = run.get("tool")
        driver = tool.get("driver") if isinstance(tool, dict) else None
        if not isinstance(driver, dict) or not isinstance(driver.get("name"), str):
            raise ValueError("SARIF requires a named tool driver")
        rules = driver.get("rules", [])
        results = run.get("results", [])
        if (
            not isinstance(rules, list)
            or len(rules) > MAX_FINDINGS
            or not isinstance(results, list)
            or len(results) + len(imported) > MAX_FINDINGS
        ):
            raise ValueError("SARIF rules or results exceed limits")
        by_id = {}
        for descriptor in rules:
            if not isinstance(descriptor, dict):
                raise ValueError("Invalid SARIF rule descriptor")
            identifier = descriptor.get("id")
            if (
                not isinstance(identifier, str)
                or not identifier
                or len(identifier) > 200
                or identifier in by_id
            ):
                raise ValueError("SARIF rules need distinct bounded identifiers")
            by_id[identifier] = descriptor
        for result in results:
            if not isinstance(result, dict):
                raise ValueError("Invalid SARIF result")
            index = result.get("ruleIndex")
            rule = {}
            if index is not None:
                if type(index) is not int or not -1 <= index < len(rules):
                    raise ValueError("Invalid SARIF rule index")
                if index >= 0:
                    rule = rules[index]
            rule_id = result.get("ruleId", rule.get("id", "security-finding"))
            if not isinstance(rule_id, str):
                raise ValueError("SARIF rule ID must be text")
            if (
                index is not None
                and index >= 0
                and result.get("ruleId", rule.get("id")) != rule.get("id")
            ):
                raise ValueError("SARIF rule ID and index disagree")
            if not rule:
                rule = by_id.get(rule_id, {})
            message = result.get("message")
            if not isinstance(message, dict) or not isinstance(
                message.get("text"), str
            ):
                raise ValueError("SARIF import requires a plain text result message")
            properties = result.get("properties", {})
            if not isinstance(properties, dict):
                raise ValueError("Invalid SARIF result properties")
            default = rule.get("defaultConfiguration", {})
            if not isinstance(default, dict):
                raise ValueError("Invalid SARIF rule configuration")
            level = result.get("level", default.get("level", "warning"))
            if not isinstance(level, str) or level not in {
                "error",
                "warning",
                "note",
                "none",
            }:
                raise ValueError("Invalid SARIF result level")
            row = {
                "id": properties.get("id", ""),
                "title": properties.get("title", message["text"][:500]),
                "description": message["text"],
                "rule": rule_id,
                "severity": {
                    "error": "HIGH",
                    "warning": "MEDIUM",
                    "note": "LOW",
                    "none": "INFO",
                }[level],
                "status": "suspected",
                "verification": "unverified",
                "exploit_path": properties.get("exploit_path", ""),
                "fix": properties.get("fix", ""),
            }
            suppressions = result.get("suppressions", [])
            if not isinstance(suppressions, list) or len(suppressions) > 100:
                raise ValueError("SARIF suppressions must be a bounded list")
            accepted = [item for item in suppressions if isinstance(item, dict) and item.get("status") == "accepted"]
            if properties.get("status") in {"false_positive", "suppressed"} or accepted:
                row["status"] = "false_positive"
                row["suppression"] = properties.get("suppression") or (
                    accepted[0].get("justification", "Imported accepted suppression") if accepted else
                    "Imported false-positive decision")
            if "package" in properties:
                row["package"] = properties["package"]
            if properties.get("source_sha256") is not None:
                row["source_sha256"] = properties["source_sha256"]
                row["source_status"] = "unavailable"
            supplied_severity = properties.get("severity")
            severity_levels = {
                "CRITICAL": "error",
                "HIGH": "error",
                "MEDIUM": "warning",
                "LOW": "note",
                "INFO": "note",
            }
            if (
                isinstance(supplied_severity, str)
                and severity_levels.get(supplied_severity) == level
            ):
                row["severity"] = supplied_severity
            if properties.get("cwe"):
                row["cwe"] = properties["cwe"]
            else:
                metadata = rule.get("properties", {})
                tags = metadata.get("tags", []) if isinstance(metadata, dict) else []
                if isinstance(tags, list):
                    for tag in tags:
                        if isinstance(tag, str) and re.fullmatch(
                            r"CWE-[1-9][0-9]{0,4}", tag
                        ):
                            row["cwe"] = tag
                            break
            locations = result.get("locations", [])
            if not isinstance(locations, list) or len(locations) > 100:
                raise ValueError("Invalid SARIF locations")
            for location in locations:
                if not isinstance(location, dict):
                    raise ValueError("Invalid SARIF location")
                physical = location.get("physicalLocation", {})
                if not isinstance(physical, dict):
                    raise ValueError("Invalid SARIF physical location")
                artifact = physical.get("artifactLocation", {})
                if (
                    not isinstance(artifact, dict)
                    or artifact.get("uriBaseId")
                    or "index" in artifact
                ):
                    raise ValueError(
                        "SARIF artifact paths must be explicit repository paths"
                    )
                path = _path(artifact["uri"]) if "uri" in artifact else ""
                region = physical.get("region", {})
                if not isinstance(region, dict):
                    raise ValueError("Invalid SARIF region")
                line = region.get("startLine", 0)
                if (
                    type(line) is not int
                    or line < 0
                    or ("startLine" in region and line == 0)
                ):
                    raise ValueError("Invalid SARIF start line")
                if path and "path" not in row:
                    row.update(path=path, line=line)
            imported.append(_findings([row])[0])
    return imported


def _markdown(value):
    value = html.escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~:/-])", r"\\\1", value)


def findings_report(findings: list[dict], coverage: dict | None = None) -> str:
    rows = _findings(findings)
    lines = [
        "# Security findings",
        "",
        f"Recorded findings: {len(rows)}.",
        "",
        "Verification labels describe recorded observations; they do not certify exploitability.",
        "",
    ]
    if coverage is None:
        lines += ["Coverage: unknown. Empty output does not prove safety.", ""]
    else:
        _bounded(coverage, MAX_RECORD_BYTES)
        lines += ["## Coverage", "", "Empty output does not prove safety.", ""]
        for key in ("reviewed", "skipped", "failed", "unknown", "entry_points", "vulnerability_classes", "gaps"):
            lines += [f"{key}: {_markdown(_canonical(coverage.get(key, 'unknown')).decode())}", ""]
    for number, row in enumerate(rows, 1):
        lines += [
            f"## {number}. {_markdown(row['title'])}",
            "",
            f"Severity: {row['severity']}. Status: {_markdown(row['status'])}. Recorded verification: {_markdown(row['verification'])}.",
            "",
        ]
        if row["path"]:
            lines += [
                f"Location: {_markdown(row['path'])}:{row['line'] or 'unspecified'}.",
                "",
            ]
        if row["cwe"]:
            lines += [f"Classification: {row['cwe']} ({row['cwe_source']}).", ""]
        lines += [_markdown(row["source_qualification"]), ""]
        if row["suppression"]:
            lines += [f"Suppression: {_markdown(row['suppression'])}", ""]
        lines += ["### Finding package (recorded claims)", "",
                  _markdown(_canonical(row["package"]).decode()), ""]
        for label, key in (
            ("Recorded source SHA-256", "source_sha256"),
            ("Audit ledger record SHA-256", "audit_record_hash"),
            ("Audit recorded at", "audit_recorded_at"),
        ):
            if row[key]:
                lines += [f"{label}: {_markdown(row[key])}", ""]
        for label, key in (
            ("Summary", "description"),
            ("Reproduction evidence", "exploit_path"),
            ("Remediation", "fix"),
        ):
            if row[key]:
                lines += [f"### {label}", "", _markdown(row[key]), ""]
    report = "\n".join(lines)
    if len(report.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError("Markdown report exceeds size limit")
    return report


def reproducibility_summary(attempts: list[dict]) -> dict:
    """Count caller-reported repeated outcomes; never trust a `verified` flag.

    Compatible attempts need identical finding, case SHA-256, and evidence
    SHA-256. IDs deduplicate attempts within that group; unidentifiable attempts
    are excluded. Hashes identify evidence but cannot prove a test was executed.
    """
    _bounded(attempts)
    if not isinstance(attempts, list) or len(attempts) > MAX_RECORDS:
        raise ValueError("Attempts must be a bounded list")
    groups, seen, eligible, duplicates = {}, set(), 0, 0
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("Each attempt must be an object")
        finding = attempt.get("finding_id")
        case, evidence = attempt.get("case_hash"), attempt.get("evidence_hash")
        identity = attempt.get("id")
        outcome = attempt.get("outcome")
        if (
            not isinstance(finding, str)
            or not finding
            or len(finding) > 200
            or not isinstance(identity, str)
            or not identity
            or len(identity) > 200
            or not isinstance(case, str)
            or not _HASH.fullmatch(case)
            or not isinstance(evidence, str)
            or not _HASH.fullmatch(evidence)
            or not isinstance(outcome, str)
            or outcome not in {"triggered", "not_triggered", "error"}
        ):
            continue
        key = (finding, case, evidence)
        if (*key, identity) in seen:
            duplicates += 1
            continue
        seen.add((*key, identity))
        eligible += 1
        group = groups.setdefault(
            key,
            {
                "finding_id": _text(finding, 200),
                "case_hash": case,
                "evidence_hash": evidence,
                "attempts": 0,
                "triggered": 0,
                "not_triggered": 0,
                "error": 0,
            },
        )
        group["attempts"] += 1
        group[outcome] += 1
    for group in groups.values():
        group["reproducibility"] = group["triggered"] / group["attempts"]
    return {
        "verification": "reported",
        "reported_attempts": len(attempts),
        "eligible_attempts": eligible,
        "duplicate_attempts": duplicates,
        "excluded_attempts": len(attempts) - eligible - duplicates,
        "verified_attempts": 0,
        "groups": list(groups.values()),
    }

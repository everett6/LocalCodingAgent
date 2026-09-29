"""Triage of scanner findings: fingerprint, dedupe, suppress, and mark new.

Fingerprints hash the rule, the file and the flagged line's text rather than
its number, so a finding keeps its identity when code above it moves (the
same idea as code-scanning "partial fingerprints"). The baseline is the set of
fingerprints the last completed review saw; anything outside it is new.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from local_coder.security.lessons import STATE_DIR, Lesson

BASELINE_FILE = f"{STATE_DIR}/baseline.json"
LAST_SCAN_FILE = f"{STATE_DIR}/last-scan.json"
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


class ScanFinding(Protocol):
    path: str
    line: int
    severity: str
    scanner: str
    rule: str
    message: str


@dataclass
class TriageResult:
    findings: list = field(default_factory=list)
    suppressed: list[tuple[object, Lesson]] = field(default_factory=list)
    new: set[str] = field(default_factory=set)  # fingerprints not in the baseline
    fingerprints: dict[int, str] = field(default_factory=dict)  # id(finding) -> fingerprint
    has_baseline: bool = False

    def is_new(self, finding) -> bool:
        return self.fingerprints.get(id(finding)) in self.new


def _line_text(root: Path, rel: str, line: int, cache: dict[str, list[str]]) -> str:
    if rel not in cache:
        try:
            cache[rel] = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[rel] = []
    lines = cache[rel]
    return lines[line - 1] if 0 < line <= len(lines) else ""


def fingerprint(scanner: str, rule: str, path: str, line_text: str, line: int) -> str:
    anchor = " ".join(line_text.split()) or f"line {line}"
    return hashlib.sha1(f"{scanner}/{rule}|{path}|{anchor}".encode()).hexdigest()[:16]


def dedupe(findings: list) -> list:
    """Collapse findings several scanners report on the same path:line,
    keeping the most severe and naming every scanner that saw it."""
    by_location: dict[tuple[str, int], object] = {}
    for finding in findings:
        key = (finding.path, finding.line)
        current = by_location.get(key)
        if current is None:
            by_location[key] = finding
            continue
        scanners = sorted(set(current.scanner.split("+")) | set(finding.scanner.split("+")))
        keep = finding if SEVERITY_ORDER.get(finding.severity, 9) < SEVERITY_ORDER.get(current.severity, 9) else current
        by_location[key] = replace(keep, scanner="+".join(scanners))
    return list(by_location.values())


def load_fingerprints(path: Path) -> set[str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return set(data) if isinstance(data, list) else None


def save_fingerprints(path: Path, fingerprints: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(fingerprints)), encoding="utf-8")


def triage(findings: list, root: Path, suppressions: list[Lesson], baseline: set[str] | None) -> TriageResult:
    result = TriageResult(has_baseline=baseline is not None)
    cache: dict[str, list[str]] = {}
    for finding in dedupe(findings):
        lesson = next(
            (s for s in suppressions if any(s.matches(scanner, finding.rule, finding.path)
                                             for scanner in finding.scanner.split("+"))),
            None,
        )
        if lesson is not None:
            result.suppressed.append((finding, lesson))
            continue
        # Fingerprint with the first scanner name so a finding that bandit and
        # semgrep both report keeps one identity whichever ran.
        scanner = finding.scanner.split("+")[0]
        fp = fingerprint(scanner, finding.rule, finding.path, _line_text(root, finding.path, finding.line, cache), finding.line)
        result.fingerprints[id(finding)] = fp
        if baseline is not None and fp not in baseline:
            result.new.add(fp)
        result.findings.append(finding)
    result.findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), not result.is_new(f), f.path, f.line))
    return result


def promote_last_scan(project_root: str | Path) -> bool:
    """Make the latest scan the baseline for the next review. Called when a
    review finishes, so scans inside one review all compare to the previous one."""
    root = Path(project_root)
    fingerprints = load_fingerprints(root / LAST_SCAN_FILE)
    if fingerprints is None:
        return False
    save_fingerprints(root / BASELINE_FILE, fingerprints)
    return True

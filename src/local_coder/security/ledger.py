"""The findings ledger: structured notes a security review keeps as it works.

A long review compacts its conversation, and a summary written by a small
local model is exactly where a file:line or an exploit path gets lost. So the
reviewer records each finding with the record_finding tool; the ledger lives
on disk, outside the chat history, and the agent re-pins a compact rendering
of it into the conversation after every compaction. Batched reviews hand it
from one batch to the next the same way.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from local_coder.file_lock import exclusive_lock
from local_coder.statedir import state_path
from local_coder.security.lessons import STATE_DIR, Lesson

LEDGER_FILE = f"{STATE_DIR}/ledger.json"
STATUSES = ("suspected", "confirmed", "false_positive")
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
REPORT_MIN_CONFIDENCE = 8


@dataclass
class LedgerEntry:
    title: str
    severity: str
    path: str
    line: int = 0
    rule: str = ""
    status: str = "suspected"
    confidence: int = 5
    exploit_path: str = ""
    fix: str = ""
    lesson: str = ""
    id: str = ""

    @property
    def key(self) -> str:
        # The same issue reported twice (by two batches, or once more after a
        # compaction) updates one entry instead of adding a duplicate.
        subject = self.rule or re.sub(r"[^a-z0-9]+", " ", self.title.lower()).strip()
        return f"{self.path}|{subject}"

    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path

    def headline(self) -> str:
        head = f"{self.id} [{self.severity} conf {self.confidence}/10 {self.status}] {self.location()} {self.title}"
        return head + (f" ({self.rule})" if self.rule else "")

    def render(self, detail_chars: int = 240) -> str:
        def clip(text: str) -> str:
            text = " ".join(text.split())
            return text if len(text) <= detail_chars else text[: detail_chars - 1] + "…"

        parts = [self.headline()]
        if detail_chars <= 0:
            return parts[0]
        if self.exploit_path:
            parts.append(f"exploit: {clip(self.exploit_path)}")
        if self.fix:
            parts.append(f"fix: {clip(self.fix)}")
        return " | ".join(parts)

    def sort_key(self) -> tuple:
        return (STATUSES.index(self.status) == 2, SEVERITY_RANK.get(self.severity, 9), -self.confidence, self.path, self.line)


@dataclass
class FindingsLedger:
    path: Path
    entries: list[LedgerEntry] = field(default_factory=list)
    _project_root: Path | None = field(default=None, repr=False)

    @classmethod
    def for_project(cls, project_root: str | Path) -> "FindingsLedger":
        root = Path(project_root).resolve()
        ledger = cls(root / LEDGER_FILE, _project_root=root)
        ledger.load()
        return ledger

    def _safe_path(self, name: str) -> Path:
        root = self._project_root or self.path.parent
        relative = self.path.parent.relative_to(root) / name
        return state_path(root, relative)

    @contextmanager
    def _locked(self):
        lock_path = self._safe_path(self.path.name + ".lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(lock_path, flags, 0o600), "a+") as lock_file:
            with exclusive_lock(lock_file):
                yield

    def load(self) -> None:
        with self._locked():
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        path = self._safe_path(self.path.name)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as source:
                data = json.load(source)
        except FileNotFoundError:
            self.entries = []
            return
        except (OSError, ValueError) as exc:
            raise ValueError("Findings ledger is unreadable; preserve it and restore a valid copy") from exc
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Findings ledger must contain a list of finding objects")
        fields = set(LedgerEntry.__dataclass_fields__)
        try:
            entries = [LedgerEntry(**{k: v for k, v in item.items() if k in fields}) for item in data]
            self._validate_entries(entries)
        except (TypeError, ValueError) as exc:
            raise ValueError("Findings ledger contains invalid records; preserve it before recovery") from exc
        self.entries = entries

    @staticmethod
    def _validate_entries(entries: list[LedgerEntry]) -> None:
        fields = set(LedgerEntry.__dataclass_fields__)
        for entry in entries:
            if (entry.status not in STATUSES or entry.severity not in SEVERITIES
                    or type(entry.line) is not int or entry.line < 0
                    or type(entry.confidence) is not int or not 1 <= entry.confidence <= 10
                    or any(not isinstance(getattr(entry, name), str) for name in fields - {"line", "confidence"})):
                raise ValueError("Invalid finding fields")

    def save(self) -> None:
        """Replace the snapshot explicitly; use record() for concurrent updates."""
        with self._locked():
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        self._validate_entries(self.entries)
        path = self._safe_path(self.path.name)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix=".ledger-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump([asdict(entry) for entry in self.entries], output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            self._safe_path(self.path.name)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def reset(self) -> None:
        with self._locked():
            self.entries = []
            self._save_unlocked()

    def record(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        """Merge against the latest disk state under a cross-process lock."""
        with self._locked():
            self._load_unlocked()
            return self._record_unlocked(entry)

    def _record_unlocked(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        for index, existing in enumerate(self.entries):
            if existing.key == entry.key or (entry.id and existing.id == entry.id):
                merged = LedgerEntry(**{
                    name: (value if value not in ("", 0, None) else getattr(existing, name))
                    for name, value in asdict(entry).items()
                })
                merged.id = existing.id
                self.entries[index] = merged
                self._save_unlocked()
                return merged, False
        entry.status = entry.status or "suspected"
        entry.confidence = entry.confidence or 5
        entry.id = f"F{len(self.entries) + 1}"
        self.entries.append(entry)
        self._save_unlocked()
        return entry, True

    def reportable(self, min_confidence: int = REPORT_MIN_CONFIDENCE) -> list[LedgerEntry]:
        return sorted(
            (e for e in self.entries if e.status != "false_positive"
             and (e.status == "confirmed" or e.confidence >= min_confidence)),
            key=LedgerEntry.sort_key,
        )

    def render(self, max_chars: int = 4000) -> str:
        """Compact, severity-ordered view that fits in max_chars. Low-value
        entries go first when space runs out, never the most severe ones."""
        if not self.entries:
            return ""
        ordered = sorted(self.entries, key=LedgerEntry.sort_key)
        for detail in (240, 120, 0):
            lines = [e.render(detail) for e in ordered]
            text = "\n".join(lines)
            if len(text) <= max_chars:
                return text
        kept: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) + 1 > max_chars - 60:
                break
            kept.append(line)
            used += len(line) + 1
        return "\n".join(kept) + f"\n...[{len(lines) - len(kept)} lower-priority findings omitted]"

    def proposed_lessons(self) -> list[Lesson]:
        """Lessons worth keeping for future reviews. Only proposals: a person
        accepts them into SECURITY_LESSONS.md."""
        lessons: list[Lesson] = []
        for entry in self.entries:
            if entry.status == "false_positive" and entry.rule:
                lessons.append(Lesson("suppress", entry.lesson or f"False positive: {entry.title}", entry.rule, entry.path))
            elif entry.status == "confirmed":
                note = entry.title + (f"; fix: {' '.join(entry.fix.split())[:160]}" if entry.fix else "")
                lessons.append(Lesson("confirmed", note, entry.rule, entry.path))
            if entry.lesson and (entry.status != "false_positive" or not entry.rule):
                lessons.append(Lesson("pattern", entry.lesson))
        return lessons


def render_ledger_report(ledger: FindingsLedger, batches: int, files: int, notes: list[str], note_chars: int = 1200) -> str:
    """Final report for a batched review, built from the ledger rather than
    from any one batch's conversation, so nothing a batch recorded is lost."""
    reportable = ledger.reportable()
    below = [e for e in ledger.entries if e.status != "false_positive" and e not in reportable]
    dismissed = [e for e in ledger.entries if e.status == "false_positive"]
    worst = reportable[0].severity.lower() if reportable else None
    summary = (
        f"{len(reportable)} finding(s) worth fixing, worst {worst}" if worst else "No findings met the reporting bar"
    ) + f" ({files} files reviewed in {batches} batches)."
    parts = ["# Security review", summary]
    if reportable:
        parts.append("## Findings")
        for entry in reportable:
            block = [f"### {entry.severity.title()}: {entry.title}", f"- Location: `{entry.location()}`"
                     + (f" (rule {entry.rule})" if entry.rule else ""),
                     f"- Status: {entry.status}, confidence {entry.confidence}/10"]
            if entry.exploit_path:
                block.append(f"- Exploit path: {entry.exploit_path}")
            if entry.fix:
                block.append(f"- Fix: {entry.fix}")
            parts.append("\n".join(block))
    if below:
        parts.append(f"## Below the reporting bar (confidence under {REPORT_MIN_CONFIDENCE}/10)\n"
                     + "\n".join(f"- {e.headline()}" for e in sorted(below, key=LedgerEntry.sort_key)))
    if dismissed:
        parts.append("## Dismissed as false positives\n"
                     + "\n".join(f"- {e.headline()}" + (f": {e.lesson}" if e.lesson else "") for e in dismissed))
    batch_notes = []
    for number, note in enumerate(notes, start=1):
        note = note.strip()
        if note:
            clipped = note if len(note) <= note_chars else note[:note_chars].rsplit("\n", 1)[0] + "\n…"
            batch_notes.append(f"### Batch {number}\n{clipped}")
    if batch_notes:
        parts.append("## Batch notes (hardening ideas and context)\n" + "\n\n".join(batch_notes))
    return "\n\n".join(parts)

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
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

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

    @classmethod
    def for_project(cls, project_root: str | Path) -> "FindingsLedger":
        ledger = cls(Path(project_root) / LEDGER_FILE)
        ledger.load()
        return ledger

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        fields = set(LedgerEntry.__dataclass_fields__)
        self.entries = [
            LedgerEntry(**{k: v for k, v in item.items() if k in fields}) for item in data if isinstance(item, dict)
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(entry) for entry in self.entries], indent=2), encoding="utf-8")

    def reset(self) -> None:
        self.entries = []
        self.save()

    def record(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        """Add or update an entry. Returns (stored entry, created)."""
        for index, existing in enumerate(self.entries):
            if existing.key == entry.key or (entry.id and existing.id == entry.id):
                merged = LedgerEntry(**{
                    name: (value if value not in ("", 0, None) else getattr(existing, name))
                    for name, value in asdict(entry).items()
                })
                merged.id = existing.id
                self.entries[index] = merged
                self.save()
                return merged, False
        entry.status = entry.status or "suspected"
        entry.confidence = entry.confidence or 5
        entry.id = f"F{len(self.entries) + 1}"
        self.entries.append(entry)
        self.save()
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

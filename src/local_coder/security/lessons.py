"""SECURITY_LESSONS.md: what earlier security reviews taught us about this project.

The file is plain markdown in the repository, so lessons are versioned,
reviewed in pull requests and shared with the team, like AGENTS.md. Three
sections, one entry per bullet:

    ## Suppress
    - secrets/hardcoded-credential | tests/** | Fixtures use fake credentials.

    ## Confirmed
    - B602 | src/jobs.py | shell=True with the job name from the request.

    ## Patterns
    - Every HTTP handler must call auth.require_user; one that does not is a finding.

Suppress and Confirmed entries are `rule | path-glob | note`. The agent never
writes this file directly: it proposes lessons, which wait in
.local-coder/security/proposed-lessons.json until a person accepts them with
`local-coder security-lessons accept`.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

LESSONS_FILE = "SECURITY_LESSONS.md"
STATE_DIR = ".local-coder/security"
PROPOSALS_FILE = f"{STATE_DIR}/proposed-lessons.json"

KINDS = ("suppress", "confirmed", "pattern")
SECTION_TITLES = {"suppress": "Suppress", "confirmed": "Confirmed", "pattern": "Patterns"}
SECTION_KINDS = {title.lower(): kind for kind, title in SECTION_TITLES.items()}

FILE_HEADER = """# Security lessons

Reviewed notes that `local-coder security` loads on every run. Edit freely.
`local-coder security-lessons accept` appends lessons the agent proposed.

Suppress and Confirmed entries: `- rule | path-glob | note`. The rule is the
scanner rule (`secrets/hardcoded-credential`, `B602`) or `*` for any rule.
"""


@dataclass(frozen=True)
class Lesson:
    kind: str  # one of KINDS
    note: str
    rule: str = ""
    path: str = ""

    @property
    def id(self) -> str:
        """Short stable id, so a proposal can be accepted by name."""
        raw = f"{self.kind}|{self.rule}|{self.path}|{self.note.strip().lower()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:6]

    def render(self) -> str:
        if self.kind == "pattern":
            return f"- {self.note}"
        return f"- {self.rule or '*'} | {self.path or '*'} | {self.note}"

    def matches(self, scanner: str, rule: str, path: str) -> bool:
        """Whether this lesson covers a scanner finding at path."""
        wanted = self.rule or "*"
        rule_ok = wanted == "*" or wanted in {rule, f"{scanner}/{rule}"}
        return rule_ok and fnmatch.fnmatch(path, self.path or "*")


def parse_lessons(text: str) -> list[Lesson]:
    lessons: list[Lesson] = []
    kind: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            kind = SECTION_KINDS.get(line[3:].strip().lower())
            continue
        if kind is None or not line.startswith(("- ", "* ")):
            continue
        body = line[2:].strip()
        if not body:
            continue
        if kind == "pattern":
            lessons.append(Lesson("pattern", body))
            continue
        parts = [part.strip() for part in body.split("|", 2)]
        if len(parts) == 3:
            rule, path, note = parts
        elif len(parts) == 2:
            rule, path, note = parts[0], parts[1], ""
        else:
            # A bare note can't be matched against findings; keep it as context.
            rule, path, note = "*", "", parts[0]
        lessons.append(Lesson(kind, note, rule if rule != "*" else "", path if path != "*" else ""))
    return lessons


def render_lessons(lessons: list[Lesson]) -> str:
    sections = [FILE_HEADER.rstrip()]
    for kind in KINDS:
        entries = [lesson.render() for lesson in lessons if lesson.kind == kind]
        sections.append(f"## {SECTION_TITLES[kind]}\n\n" + ("\n".join(entries) if entries else ""))
    return "\n\n".join(section.rstrip() for section in sections) + "\n"


class LessonStore:
    """SECURITY_LESSONS.md plus the queue of proposals awaiting review."""

    def __init__(self, project_root: str | Path):
        self.root = Path(project_root)
        self.lessons_path = self.root / LESSONS_FILE
        self.proposals_path = self.root / PROPOSALS_FILE

    # --- accepted lessons -------------------------------------------------

    def load(self) -> list[Lesson]:
        try:
            return parse_lessons(self.lessons_path.read_text(encoding="utf-8"))
        except OSError:
            return []

    def suppressions(self) -> list[Lesson]:
        return [lesson for lesson in self.load() if lesson.kind == "suppress"]

    def render_for_prompt(self, max_chars: int = 4000) -> str:
        """The lessons as the agent sees them, capped to max_chars."""
        lessons = self.load()
        if not lessons:
            return ""
        blocks = []
        for kind, heading in (
            ("pattern", "Project patterns (apply them)"),
            ("confirmed", "Previously confirmed findings (check they are still fixed; do not re-report as new)"),
            ("suppress", "Known false positives (do not report these)"),
        ):
            entries = [lesson.render() for lesson in lessons if lesson.kind == kind]
            if entries:
                blocks.append(f"### {heading}\n" + "\n".join(entries))
        text = "\n\n".join(blocks)
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0] + f"\n...[truncated; see {LESSONS_FILE}]"
        return text

    def _write(self, lessons: list[Lesson]) -> None:
        if self.lessons_path.exists():
            # Append under the right headings without rewriting what a person
            # wrote by hand (comments, prose, ordering).
            text = self.lessons_path.read_text(encoding="utf-8")
            for lesson in lessons:
                text = _insert_under_heading(text, SECTION_TITLES[lesson.kind], lesson.render())
        else:
            text = render_lessons(lessons)
        self.lessons_path.write_text(text, encoding="utf-8")

    def add(self, lesson: Lesson) -> bool:
        """Add one lesson directly (a person asked for it). False if present."""
        if lesson.id in {existing.id for existing in self.load()}:
            return False
        self._write([lesson])
        return True

    # --- proposals --------------------------------------------------------

    def pending(self) -> list[Lesson]:
        try:
            data = json.loads(self.proposals_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Lesson(**item) for item in data if isinstance(item, dict) and item.get("kind") in KINDS]

    def _save_pending(self, lessons: list[Lesson]) -> None:
        self.proposals_path.parent.mkdir(parents=True, exist_ok=True)
        self.proposals_path.write_text(json.dumps([asdict(lesson) for lesson in lessons], indent=2), encoding="utf-8")

    def propose(self, lessons: list[Lesson]) -> list[Lesson]:
        """Queue lessons for review, skipping ones already accepted or queued.
        Returns the newly queued ones."""
        known = {lesson.id for lesson in self.load()}
        queue = self.pending()
        known |= {lesson.id for lesson in queue}
        added = []
        for lesson in lessons:
            if lesson.id not in known and lesson.note.strip():
                known.add(lesson.id)
                queue.append(lesson)
                added.append(lesson)
        if added:
            self._save_pending(queue)
        return added

    def _select(self, ids: list[str] | None) -> tuple[list[Lesson], list[Lesson]]:
        queue = self.pending()
        if ids is None:
            return queue, []
        wanted = set(ids)
        return [x for x in queue if x.id in wanted], [x for x in queue if x.id not in wanted]

    def accept(self, ids: list[str] | None = None) -> list[Lesson]:
        """Move proposals (all when ids is None) into SECURITY_LESSONS.md."""
        chosen, rest = self._select(ids)
        if chosen:
            self._write(chosen)
            self._save_pending(rest)
        return chosen

    def reject(self, ids: list[str] | None = None) -> list[Lesson]:
        chosen, rest = self._select(ids)
        if chosen:
            self._save_pending(rest)
        return chosen


def _insert_under_heading(text: str, title: str, entry: str) -> str:
    lines = text.rstrip("\n").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == f"## {title.lower()}"), None)
    if start is None:
        return "\n".join(lines) + f"\n\n## {title}\n\n{entry}\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    # Insert after the section's last non-blank line.
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    if insert_at == start + 1:
        lines[insert_at:insert_at] = ["", entry]
    else:
        lines.insert(insert_at, entry)
    return "\n".join(lines) + "\n"

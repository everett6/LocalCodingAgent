"""Split a large security review into batches that each fit one context.

A review of a big codebase would otherwise run one conversation until it
compacts again and again. Instead each batch starts a fresh conversation
seeded with the findings ledger so far, the way a long-running agent hands
off through a progress file rather than an ever-growing transcript. Files
most likely to hold attacker-reachable sinks go first, so if a review is cut
short the riskiest code has already been read.
"""
from __future__ import annotations

import re
from pathlib import Path

from local_coder.tools.quality import EXCLUDED_DIRS

SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift", ".scala", ".sh", ".bash", ".sql",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".json", ".xml", ".tf", ".dockerfile",
}
SOURCE_NAMES = {"Dockerfile", "Makefile", "Procfile"}
MAX_FILE_BYTES = 400_000

# Weighted hints of attacker-reachable input or a dangerous sink. Only used to
# order the review; a file with no hits is still reviewed.
RISK_HINTS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (5, re.compile(r"\b(subprocess|os\.system|popen|exec\(|eval\(|shell\s*=\s*True|child_process)")),
    (5, re.compile(r"\b(pickle\.loads?|yaml\.load\(|marshal\.loads|unserialize|ObjectInputStream)")),
    (4, re.compile(r"\b(execute\(|executemany|raw\(|cursor\.|SELECT\s|INSERT\s|UPDATE\s|DELETE\s)", re.I)),
    (4, re.compile(r"(@app\.|@router\.|@bp\.|\.route\(|request\.|req\.(body|query|params)|FastAPI|Flask|express\()")),
    (3, re.compile(r"\b(open\(|send_file|Path\(|os\.path\.join|readFile|writeFile)")),
    (3, re.compile(r"\b(auth|token|password|secret|session|cookie|jwt|login|permission)", re.I)),
    (2, re.compile(r"\b(requests\.|urllib|httpx|fetch\(|axios|socket)")),
    (2, re.compile(r"\b(md5|sha1|random\.|DES|ECB|verify\s*=\s*False)")),
)
LOW_PRIORITY_PARTS = {"tests", "test", "docs", "examples", "fixtures", "benchmarks"}


def collect_files(root: Path, paths: list[str] | None = None) -> list[str]:
    """Source and config files under paths (default: the whole project)."""
    files: list[str] = []
    for target in [root / p for p in (paths or ["."])]:
        candidates = [target] if target.is_file() else sorted(target.rglob("*")) if target.is_dir() else []
        for path in candidates:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part in EXCLUDED_DIRS or (part.startswith(".") and part != ".github") for part in rel.parts[:-1]):
                continue
            if not path.is_file() or (path.suffix.lower() not in SOURCE_SUFFIXES and path.name not in SOURCE_NAMES):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(str(rel))
    return list(dict.fromkeys(files))


def risk_score(root: Path, rel: str) -> int:
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    score = sum(weight * min(len(pattern.findall(text)), 5) for weight, pattern in RISK_HINTS)
    if LOW_PRIORITY_PARTS & set(Path(rel).parts[:-1]):
        score //= 4
    return score


def plan_batches(root: Path, files: list[str], batch_chars: int) -> list[list[str]]:
    """Group files, riskiest first, into batches of about batch_chars of source."""
    ranked = sorted(files, key=lambda rel: (-risk_score(root, rel), rel))
    batches: list[list[str]] = []
    current: list[str] = []
    used = 0
    for rel in ranked:
        try:
            size = (root / rel).stat().st_size
        except OSError:
            continue
        if current and used + size > batch_chars:
            batches.append(current)
            current, used = [], 0
        current.append(rel)
        used += size
    if current:
        batches.append(current)
    return batches


def total_chars(root: Path, files: list[str]) -> int:
    total = 0
    for rel in files:
        try:
            total += (root / rel).stat().st_size
        except OSError:
            pass
    return total

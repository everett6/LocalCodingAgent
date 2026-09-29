"""Local code retrieval index.

A dependency-free BM25 index over code chunks. Python files are split into
functions, methods and classes with ``ast``; other languages are split at
definition lines found by a regex; anything else is cut into fixed windows.
Identifiers are split on snake_case and camelCase so a query like
"parse config" finds ``parseConfig`` and ``parse_config``, and matches in a
chunk's symbol name or file path rank higher than matches in its body.

The index lives in ``.local-coder/index/code_index.json`` and is refreshed
incrementally: only files whose size or mtime changed are re-read.
"""
from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

INDEX_VERSION = 1
INDEX_RELPATH = Path(".local-coder") / "index" / "code_index.json"

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 20000
MAX_CHUNK_LINES = 80
WINDOW_LINES = 50

# BM25 parameters and field boosts
K1 = 1.2
B = 0.75
SYMBOL_BOOST = 1.5
PATH_BOOST = 0.5

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".eggs", ".local-coder",
    ".next", ".nuxt", "target", "vendor",
}

SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll", ".exe", ".class", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".bmp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".db", ".sqlite", ".sqlite3", ".bin", ".lock", ".map",
    ".mp3", ".mp4", ".wav", ".mov",
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "how", "if", "in", "into", "is", "it", "its", "of", "on", "or",
    "that", "the", "this", "to", "was", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "does", "do", "can", "should",
    # language noise that appears in nearly every chunk
    "self", "def", "return", "import", "none", "true", "false", "not",
    "let", "var", "const", "else", "elif", "pass", "null", "nil", "new",
}

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Definition lines for common non-Python languages (JS/TS, Go, Rust, Java,
# C#, Kotlin, Swift, Ruby, PHP, shell functions).
_DEF_RE = re.compile(
    r"^\s*(?:(?:export|default|public|private|protected|internal|static|async|"
    r"abstract|final|override|pub(?:\([a-z]+\))?|unsafe|extern|inline|virtual|"
    r"open|data|sealed)\s+)*"
    r"(?:function\*?|class|interface|struct|enum|trait|impl|fn|func|def|module|"
    r"type|object|protocol|extension|fun)\s+"
    r"(?:\([^)]*\)\s*)?"  # Go method receiver
    r"([A-Za-z_$][\w$]*)"
)
_ASSIGNED_FN_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
)
_CODE_EXTENSIONS = {
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java",
    ".kt", ".kts", ".scala", ".cs", ".swift", ".rb", ".php", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".sh", ".bash", ".lua", ".dart",
}


def _stem(word: str) -> str:
    """Very light suffix stripping so parse/parsing/parsed/parses all meet."""
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("es"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Split text into search terms, expanding compound identifiers.

    ``parseConfigFile`` yields ``parseconfigfile``, ``parse``, ``config`` and
    ``file``; ``read_file_safe`` yields the whole name plus its parts.
    """
    tokens: list[str] = []
    for word in _WORD_RE.findall(text):
        parts: list[str] = []
        for piece in word.split("_"):
            if piece:
                parts.extend(_CAMEL_RE.findall(piece))
        lowered = word.lower().strip("_")
        if len(parts) > 1 and lowered not in STOPWORDS:
            tokens.append(lowered)
        for part in parts:
            p = part.lower()
            if len(p) < 2 or p in STOPWORDS or p.isdigit():
                continue
            tokens.append(_stem(p))
    return tokens


@dataclass
class Chunk:
    path: str
    start: int  # 1-based, inclusive
    end: int  # 1-based, inclusive
    kind: str  # function | class | module | block
    symbol: str = ""
    tf: dict[str, int] = field(default_factory=dict)
    length: int = 0
    symbol_terms: frozenset[str] = frozenset()
    path_terms: frozenset[str] = frozenset()

    def to_json(self) -> list:
        return [self.start, self.end, self.kind, self.symbol, self.tf]


@dataclass
class SearchHit:
    path: str
    start: int
    end: int
    kind: str
    symbol: str
    score: float
    snippet: list[tuple[int, str]] = field(default_factory=list)

    def location(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


# === Chunking ===

def _python_spans(source: str) -> list[tuple[int, int, str, str]] | None:
    """Return (start, end, kind, symbol) spans for a Python module."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    spans: list[tuple[int, int, str, str]] = []

    def start_of(node: ast.AST) -> int:
        decorators = getattr(node, "decorator_list", [])
        return min([node.lineno] + [d.lineno for d in decorators])

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spans.append((start_of(node), node.end_lineno or node.lineno,
                              "function", prefix + node.name))
            elif isinstance(node, ast.ClassDef):
                name = prefix + node.name
                methods = [
                    n for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                ]
                end = node.end_lineno or node.lineno
                # The class chunk holds the header, docstring and attributes;
                # each method becomes its own chunk.
                header_end = (start_of(methods[0]) - 1) if methods else end
                spans.append((start_of(node), max(header_end, node.lineno), "class", name))
                visit(node.body, name + ".")

    visit(tree.body, "")
    return spans


def _regex_spans(lines: list[str]) -> list[tuple[int, int, str, str]]:
    """Split at definition lines; each span runs to the next definition."""
    starts: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines, start=1):
        m = _DEF_RE.match(line)
        if m:
            kind = "class" if re.search(
                r"\b(class|interface|struct|enum|trait|impl|type|object|protocol)\s", line
            ) else "function"
            starts.append((i, kind, m.group(1)))
            continue
        m = _ASSIGNED_FN_RE.match(line)
        if m:
            starts.append((i, "function", m.group(1)))

    spans = []
    for idx, (start, kind, name) in enumerate(starts):
        end = starts[idx + 1][0] - 1 if idx + 1 < len(starts) else len(lines)
        spans.append((start, end, kind, name))
    return spans


def _split_long(start: int, end: int, kind: str, symbol: str) -> list[tuple[int, int, str, str]]:
    if end - start + 1 <= MAX_CHUNK_LINES:
        return [(start, end, kind, symbol)]
    out = []
    for s in range(start, end + 1, MAX_CHUNK_LINES):
        out.append((s, min(s + MAX_CHUNK_LINES - 1, end), kind, symbol))
    return out


def chunk_file(path: str, text: str) -> list[Chunk]:
    """Split a file into indexable chunks."""
    lines = text.splitlines()
    if not lines:
        return []
    ext = os.path.splitext(path)[1].lower()

    spans: list[tuple[int, int, str, str]] | None = None
    if ext in (".py", ".pyi"):
        spans = _python_spans(text)
    elif ext in _CODE_EXTENSIONS:
        spans = _regex_spans(lines)

    covered = [False] * (len(lines) + 1)
    pieces: list[tuple[int, int, str, str]] = []
    for start, end, kind, symbol in spans or []:
        end = min(end, len(lines))
        if end < start:
            continue
        pieces.extend(_split_long(start, end, kind, symbol))
        for i in range(start, end + 1):
            covered[i] = True

    # Whatever no definition covers (imports, module constants, prose in
    # non-code files) is grouped into contiguous windows.
    run_start = None
    for i in range(1, len(lines) + 2):
        is_free = i <= len(lines) and not covered[i]
        if is_free and run_start is None:
            run_start = i
        elif not is_free and run_start is not None:
            for s in range(run_start, i, WINDOW_LINES):
                e = min(s + WINDOW_LINES - 1, i - 1)
                if any(lines[j - 1].strip() for j in range(s, e + 1)):
                    pieces.append((s, e, "module" if spans else "block", ""))
            run_start = None

    path_terms = tokenize(path)
    chunks = []
    for start, end, kind, symbol in sorted(pieces):
        body = "\n".join(lines[start - 1:end])
        terms = tokenize(body) + tokenize(symbol) + path_terms
        if not terms:
            continue
        chunks.append(Chunk(path=path, start=start, end=end, kind=kind,
                            symbol=symbol, tf=dict(Counter(terms))))
    return chunks


# === Index ===

class CodeIndex:
    """Incrementally maintained BM25 index over a project's source files."""

    def __init__(self, project_root: str, persist: bool = True):
        self.project_root = Path(project_root).resolve()
        self.persist = persist
        self.index_path = self.project_root / INDEX_RELPATH
        self._files: dict[str, dict] = {}  # path -> {mtime_ns, size, chunks}
        self._chunks: list[Chunk] = []
        self._df: Counter[str] = Counter()
        self._avgdl = 1.0
        self._loaded = False

    # --- file discovery ---

    def _candidate_files(self) -> list[str]:
        files: list[str] | None = None
        try:
            result = subprocess.run(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=str(self.project_root), capture_output=True, timeout=15,
            )
            if result.returncode == 0:
                files = [f for f in result.stdout.decode("utf-8", "replace").split("\0") if f]
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            files = None

        if files is None:
            files = []
            for root, dirs, names in os.walk(self.project_root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
                for name in names:
                    rel = os.path.relpath(os.path.join(root, name), self.project_root)
                    files.append(rel)

        out = []
        for rel in files:
            rel = rel.replace(os.sep, "/")
            parts = rel.split("/")
            if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
                continue
            if os.path.splitext(rel)[1].lower() in SKIP_EXTENSIONS:
                continue
            out.append(rel)
            if len(out) >= MAX_FILES:
                break
        return out

    def _read_text(self, full: Path) -> str | None:
        try:
            data = full.read_bytes()
        except OSError:
            return None
        if b"\0" in data[:4096]:
            return None
        return data.decode("utf-8", errors="replace")

    # --- persistence ---

    def _load(self) -> None:
        self._loaded = True
        if not self.persist or not self.index_path.is_file():
            return
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if data.get("version") != INDEX_VERSION:
            return
        files = data.get("files")
        if isinstance(files, dict):
            self._files = files

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": INDEX_VERSION, "files": self._files}), "utf-8")
            os.replace(tmp, self.index_path)
        except OSError as e:
            logger.debug("Could not persist code index: %s", e)

    # --- build / refresh ---

    def refresh(self) -> dict[str, int]:
        """Bring the index up to date with the working tree.

        Returns counts of files that were added/updated, removed, and kept.
        """
        if not self._loaded:
            self._load()

        stats = {"updated": 0, "removed": 0, "unchanged": 0}
        seen: set[str] = set()
        for rel in self._candidate_files():
            full = self.project_root / rel
            try:
                st = full.stat()
            except OSError:
                continue
            if not full.is_file() or st.st_size > MAX_FILE_BYTES:
                continue
            seen.add(rel)
            entry = self._files.get(rel)
            if entry and entry.get("mtime_ns") == st.st_mtime_ns and entry.get("size") == st.st_size:
                stats["unchanged"] += 1
                continue
            text = self._read_text(full)
            chunks = chunk_file(rel, text) if text is not None else []
            self._files[rel] = {
                "mtime_ns": st.st_mtime_ns,
                "size": st.st_size,
                "chunks": [c.to_json() for c in chunks],
            }
            stats["updated"] += 1

        for rel in list(self._files):
            if rel not in seen:
                del self._files[rel]
                stats["removed"] += 1

        if stats["updated"] or stats["removed"] or not self._chunks:
            self._rebuild_stats()
        if stats["updated"] or stats["removed"]:
            self._save()
        return stats

    def _rebuild_stats(self) -> None:
        self._chunks = []
        self._df = Counter()
        total = 0
        for rel, entry in self._files.items():
            path_terms = frozenset(tokenize(rel))
            for start, end, kind, symbol, tf in entry.get("chunks", []):
                chunk = Chunk(
                    path=rel, start=start, end=end, kind=kind, symbol=symbol, tf=tf,
                    length=sum(tf.values()),
                    symbol_terms=frozenset(tokenize(symbol)),
                    path_terms=path_terms,
                )
                self._chunks.append(chunk)
                self._df.update(tf.keys())
                total += chunk.length
        self._avgdl = (total / len(self._chunks)) if self._chunks else 1.0

    @property
    def file_count(self) -> int:
        return len(self._files)

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    # --- search ---

    def _idf(self, term: str) -> float:
        n = len(self._chunks)
        df = self._df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(
        self,
        query: str,
        max_results: int = 8,
        path_prefix: str | None = None,
        refresh: bool = True,
    ) -> list[SearchHit]:
        """Rank code chunks against a natural-language or identifier query."""
        if refresh:
            self.refresh()
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms or not self._chunks:
            return []
        prefix = os.path.normpath(path_prefix).replace(os.sep, "/") if path_prefix else ""
        if prefix == ".":
            prefix = ""
        idf = {t: self._idf(t) for t in terms}

        scored: list[tuple[float, Chunk]] = []
        for chunk in self._chunks:
            if prefix and not (chunk.path == prefix or chunk.path.startswith(prefix + "/")):
                continue
            score = 0.0
            norm = K1 * (1 - B + B * chunk.length / self._avgdl)
            for t in terms:
                f = chunk.tf.get(t)
                if not f:
                    continue
                score += idf[t] * (f * (K1 + 1)) / (f + norm)
                if t in chunk.symbol_terms:
                    score += idf[t] * SYMBOL_BOOST
                if t in chunk.path_terms:
                    score += idf[t] * PATH_BOOST
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda sc: (-sc[0], sc[1].path, sc[1].start))
        return [
            SearchHit(path=c.path, start=c.start, end=c.end, kind=c.kind,
                      symbol=c.symbol, score=round(s, 2))
            for s, c in scored[:max_results]
        ]

    def search_files(self, query: str, max_files: int = 10) -> list[str]:
        """Rank whole files: a file scores its best chunk plus a share of the rest."""
        hits = self.search(query, max_results=max(50, max_files * 5))
        by_file: dict[str, list[float]] = {}
        for hit in hits:
            by_file.setdefault(hit.path, []).append(hit.score)
        ranked = sorted(
            by_file.items(),
            key=lambda kv: (-(kv[1][0] + 0.25 * sum(kv[1][1:])), kv[0]),
        )
        return [path for path, _ in ranked[:max_files]]

    def add_snippets(self, hits: list[SearchHit], query: str, max_lines: int = 4) -> None:
        """Attach the lines of each hit that best match the query."""
        terms = set(tokenize(query))
        cache: dict[str, list[str]] = {}
        for hit in hits:
            if hit.path not in cache:
                text = self._read_text(self.project_root / hit.path) or ""
                cache[hit.path] = text.splitlines()
            lines = cache[hit.path]
            ranked = []
            for n in range(hit.start, min(hit.end, len(lines)) + 1):
                line = lines[n - 1]
                overlap = len(terms.intersection(tokenize(line)))
                if overlap:
                    ranked.append((-overlap, n))
            # Always show the chunk's first line (usually its signature),
            # then the lines sharing the most terms with the query.
            best = [n for _, n in sorted(ranked) if n != hit.start][: max_lines - 1]
            chosen = sorted({hit.start, *best}) if hit.start <= len(lines) else sorted(best)
            hit.snippet = [(n, lines[n - 1].rstrip()[:200]) for n in chosen]

    def file_chunks(self, refresh: bool = True) -> dict[str, list[list]]:
        """Snapshot of every indexed file's chunks, for the repo map.

        Each chunk is ``[start, end, kind, symbol, tf]`` as stored on disk.
        """
        if refresh:
            self.refresh()
        return {rel: list(entry.get("chunks", [])) for rel, entry in list(self._files.items())}

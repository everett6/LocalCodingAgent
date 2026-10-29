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
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread

from local_coder.statedir import state_path, write_state_file
from local_coder.context.source_snapshot import is_private_source, read_source_snapshot

logger = logging.getLogger(__name__)

INDEX_VERSION = 2
INDEX_RELPATH = Path(".local-coder") / "index" / "code_index.json"

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 20000
MAX_CHUNK_LINES = 80
WINDOW_LINES = 50
REFRESH_INTERVAL_SECONDS = 1.0

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
    source_hash: str = ""
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
        self._last_refresh_at = 0.0
        self._generation = ""
        self._watch_stop: Event | None = None
        self._watch_thread: Thread | None = None
        self._watch_ready: Event | None = None
        self._watch_error: str | None = None
        # Parallel agents share one index and search it from worker threads.
        self._lock = threading.RLock()

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
            if not self._eligible_relative(rel):
                continue
            out.append(rel)
            if len(out) >= MAX_FILES:
                break
        return out

    @staticmethod
    def _eligible_relative(rel: str) -> bool:
        path = Path(rel)
        return (not path.is_absolute() and bool(path.parts) and ".." not in path.parts
                and not is_private_source(path)
                and not any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts)
                and path.suffix.lower() not in SKIP_EXTENSIONS)

    def _inside(self, full: Path) -> bool:
        """False for a committed symlink that points outside the project."""
        return Path(os.path.realpath(full)).is_relative_to(self.project_root)

    def _read_text(self, full: Path) -> str | None:
        source = self._read_source(full)
        return source[0] if source is not None else None

    def _read_source(self, full: Path) -> tuple[str, str] | None:
        try:
            relative = full.relative_to(self.project_root).as_posix()
            if not self._eligible_relative(relative):
                return None
            snapshot = read_source_snapshot(self.project_root, relative, max_bytes=MAX_FILE_BYTES)
            data = snapshot.data
        except (OSError, ValueError):
            return None
        if b"\0" in data[:4096]:
            return None
        return data.decode("utf-8", errors="replace"), snapshot.sha256

    # --- persistence ---

    def _load(self) -> None:
        self._loaded = True
        if not self.persist or not self.index_path.exists():
            return
        try:
            cached = read_source_snapshot(self.project_root, INDEX_RELPATH, max_bytes=64 * 1024 * 1024)
            data = json.loads(cached.data)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return
        files = data.get("files")
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            return
        for rel, entry in files.items():
            if not isinstance(rel, str) or not self._eligible_relative(rel) or not isinstance(entry, dict):
                continue
            chunks = entry.get("chunks")
            digest = entry.get("content_hash")
            if (type(entry.get("mtime_ns")) is not int or type(entry.get("size")) is not int
                    or not 0 <= entry["size"] <= MAX_FILE_BYTES
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or not isinstance(chunks, list)):
                continue
            if not all(isinstance(row, list) and len(row) == 5
                       and type(row[0]) is int and type(row[1]) is int and 1 <= row[0] <= row[1] <= MAX_FILE_BYTES
                       and isinstance(row[2], str) and isinstance(row[3], str) and len(row[3]) <= MAX_FILE_BYTES
                       and isinstance(row[4], dict) and all(isinstance(term, str) and type(count) is int and count > 0
                                                          for term, count in row[4].items()) for row in chunks):
                continue
            self._files[rel] = entry

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            tmp = write_state_file(
                self.project_root, INDEX_RELPATH.with_suffix(".tmp"),
                json.dumps({"version": INDEX_VERSION, "files": self._files}),
            )
            os.replace(tmp, state_path(self.project_root, INDEX_RELPATH))
        except (OSError, ValueError) as e:
            logger.debug("Could not persist code index: %s", e)

    # --- build / refresh ---

    def refresh(self) -> dict[str, int]:
        """Bring the index up to date with the working tree.

        Returns counts of files that were added/updated, removed, and kept.
        """
        with self._lock:
            return self._refresh()

    def refresh_if_due(self) -> dict[str, int]:
        """Refresh at most once per second during repeated interactive searches."""
        with self._lock:
            now = time.monotonic()
            if self._loaded and now - self._last_refresh_at < REFRESH_INTERVAL_SECONDS:
                return {"updated": 0, "removed": 0, "unchanged": 0}
            return self._refresh()

    def _refresh(self) -> dict[str, int]:
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
            if full.is_symlink() or not full.is_file() or st.st_size > MAX_FILE_BYTES or not self._inside(full):
                continue
            entry = self._files.get(rel)
            if (entry and entry.get("mtime_ns") == st.st_mtime_ns and entry.get("size") == st.st_size
                    and entry.get("ctime_ns") == st.st_ctime_ns and entry.get("inode") == st.st_ino
                    and entry.get("device") == st.st_dev):
                seen.add(rel)
                stats["unchanged"] += 1
                continue
            source = self._read_source(full)
            if source is None:
                continue
            seen.add(rel)
            text, content_hash = source
            chunks = chunk_file(rel, text)
            self._files[rel] = {
                "mtime_ns": st.st_mtime_ns,
                "size": st.st_size,
                "ctime_ns": st.st_ctime_ns,
                "inode": st.st_ino,
                "device": st.st_dev,
                "content_hash": content_hash,
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
        self._last_refresh_at = time.monotonic()
        return stats

    def update_paths(self, paths: set[str | os.PathLike[str]]) -> dict[str, int]:
        """Re-index only changed paths received from the filesystem watcher."""
        with self._lock:
            if not self._loaded:
                self._load()
            stats = {"updated": 0, "removed": 0, "unchanged": 0}
            for path in paths:
                full = Path(path)
                if not full.is_absolute():
                    full = self.project_root / full
                full = Path(os.path.abspath(full))
                try:
                    rel = full.relative_to(self.project_root).as_posix()
                except ValueError:
                    continue
                if not self._eligible_relative(rel):
                    continue
                try:
                    stat = full.stat()
                    eligible = (
                        full.is_file()
                        and not full.is_symlink()
                        and stat.st_size <= MAX_FILE_BYTES
                        and self._inside(full)
                    )
                except OSError:
                    eligible = False
                    stat = None

                if not eligible:
                    removed = [
                        indexed for indexed in self._files
                        if indexed == rel or (not full.exists() and indexed.startswith(rel + "/"))
                    ]
                    for indexed in removed:
                        del self._files[indexed]
                    stats["removed"] += len(removed)
                    continue
                if rel not in self._files and len(self._files) >= MAX_FILES:
                    continue

                source = self._read_source(full)
                if source is None:
                    if self._files.pop(rel, None) is not None:
                        stats["removed"] += 1
                    continue
                text, content_hash = source
                chunks = chunk_file(rel, text)
                self._files[rel] = {
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "ctime_ns": stat.st_ctime_ns,
                    "inode": stat.st_ino,
                    "device": stat.st_dev,
                    "content_hash": content_hash,
                    "chunks": [chunk.to_json() for chunk in chunks],
                }
                stats["updated"] += 1

            if stats["updated"] or stats["removed"]:
                self._rebuild_stats()
                self._save()
                self._last_refresh_at = time.monotonic()
            return stats

    def start_watching(self) -> None:
        """Start a native filesystem watcher; explicit refresh remains available."""
        with self._lock:
            if self._watch_thread is not None and self._watch_thread.is_alive():
                if self._watch_stop is not None and self._watch_stop.is_set():
                    raise RuntimeError("Code index watcher is stopping")
            else:
                self._watch_stop = Event()
                self._watch_ready = Event()
                self._watch_error = None
                self._watch_thread = Thread(
                    target=self._watch_loop,
                    name=f"local-coder-index-{self.project_root.name}",
                    daemon=True,
                )
                self._watch_thread.start()
            ready = self._watch_ready
        if ready is None or not ready.wait(timeout=5):
            self.stop_watching()
            raise RuntimeError("Code index watcher did not initialize before the timeout")
        if self._watch_error:
            error = self._watch_error
            self.stop_watching()
            raise RuntimeError(f"Code index watcher failed to initialize: {error}")

    def stop_watching(self, timeout: float = 5.0) -> None:
        """Stop the native watcher and wait for its thread to exit."""
        with self._lock:
            stop = self._watch_stop
            thread = self._watch_thread
        if stop is None or thread is None:
            return
        stop.set()
        thread.join(timeout=max(0.0, timeout))
        if thread.is_alive():
            raise RuntimeError("Code index watcher did not stop before the timeout")
        with self._lock:
            self._watch_thread = None
            self._watch_stop = None
            self._watch_ready = None

    @property
    def watcher_error(self) -> str | None:
        return self._watch_error

    def _watch_loop(self) -> None:
        try:
            from watchfiles import watch

            ready = self._watch_ready
            stop = self._watch_stop
            if stop is None:
                return

            def include_change(_change, path: str) -> bool:
                candidate = Path(path)
                try:
                    relative = candidate.resolve().relative_to(self.project_root)
                except (OSError, ValueError):
                    return False
                return self._eligible_relative(relative.as_posix())

            for changes in watch(
                self.project_root,
                watch_filter=include_change,
                stop_event=stop,
                debounce=250,
                step=50,
                rust_timeout=500,
                yield_on_timeout=True,
                raise_interrupt=False,
            ):
                if stop.is_set():
                    break
                # The first yielded batch proves the native subscription is
                # active. Refresh after subscribing so edits during startup
                # cannot fall in a refresh-to-subscription gap.
                if ready is not None and not ready.is_set():
                    self.refresh()
                    ready.set()
                self.update_paths({path for _change, path in changes})
        except Exception as exc:
            self._watch_error = str(exc)
            logger.exception("Code index filesystem watcher stopped")
        finally:
            ready = self._watch_ready
            if ready is not None:
                ready.set()

    def _rebuild_stats(self) -> None:
        self._chunks = []
        self._df = Counter()
        total = 0
        revision = hashlib.sha256()
        for rel, entry in sorted(self._files.items()):
            revision.update(rel.encode("utf-8", errors="surrogateescape"))
            revision.update(b"\0")
            revision.update(entry.get("content_hash", "").encode("ascii"))
            revision.update(b"\0")
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
        self._generation = revision.hexdigest()

    @property
    def generation(self) -> str:
        """Content-derived identity for caches that must not outlive source changes."""
        with self._lock:
            return self._generation

    @property
    def file_count(self) -> int:
        with self._lock:
            return len(self._files)

    def contains_file(self, path: str) -> bool:
        with self._lock:
            return path in self._files

    @property
    def chunk_count(self) -> int:
        with self._lock:
            return len(self._chunks)

    def file_hashes(self, paths) -> dict[str, str]:
        """Snapshot provenance for indexed text, without blessing newer bytes."""
        with self._lock:
            return {path: self._files[path]["content_hash"] for path in paths if path in self._files}

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
        with self._lock:
            return self._search(query, max_results, path_prefix, refresh)

    def _search(self, query: str, max_results: int, path_prefix: str | None, refresh: bool) -> list[SearchHit]:
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
                      symbol=c.symbol, score=round(s, 2), source_hash=self._files[c.path]["content_hash"])
            for s, c in scored[:max_results]
        ]

    def search_files(self, query: str, max_files: int = 10, refresh: bool = True) -> list[str]:
        """Rank whole files: a file scores its best chunk plus a share of the rest."""
        hits = self.search(query, max_results=max(50, max_files * 5), refresh=refresh)
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
        cache: dict[str, tuple[list[str], str]] = {}
        current_hits = []
        for hit in hits:
            if hit.path not in cache:
                source = self._read_source(self.project_root / hit.path)
                text, digest = source if source is not None else ("", "")
                cache[hit.path] = (text.splitlines(), digest)
            lines, digest = cache[hit.path]
            if hit.source_hash and hit.source_hash != digest:
                continue
            current_hits.append(hit)
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
        hits[:] = current_hits

    def file_chunks(self, refresh: bool = True) -> dict[str, list[list]]:
        """Snapshot of every indexed file's chunks, for the repo map.

        Each chunk is ``[start, end, kind, symbol, tf]`` as stored on disk.
        """
        if refresh:
            self.refresh()
        with self._lock:
            return {rel: list(entry.get("chunks", [])) for rel, entry in list(self._files.items())}

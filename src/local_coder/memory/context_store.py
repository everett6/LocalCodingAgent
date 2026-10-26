"""Private, project-scoped external context memory without network dependencies.

The default vectors are deterministic lexical subword hashes, not neural semantic
embeddings. Retrieved text is untrusted evidence and never executable policy.
"""
from __future__ import annotations

import ast
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
from typing import Any, Callable, Iterator
import uuid

from local_coder.context.code_index import SKIP_DIRS, SKIP_EXTENSIONS, tokenize
from local_coder.statedir import state_path

_KINDS = {"conversation", "code", "tool", "finding", "decision", "evidence", "hypothesis", "failure"}
_STATE_FIELDS = {"objective", "constraints", "hypotheses", "failed_attempts", "evidence", "unfinished_work"}
_SENSITIVE_KEY = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key)", re.I)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)(?:bearer|basic)\s+[A-Za-z0-9+/_.=-]{8,}"),
    re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key)[\w-]*[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"),
    re.compile(r"(?i)(?:https?|postgres(?:ql)?|mysql|redis)://[^\s/@:]+:[^\s/@]+@"),
)
_PRIVATE_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}
_PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {_clean(str(key)): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise ValueError("Memory metadata and task state must contain JSON values")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identifier(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 200 or _clean(value) != value):
        raise ValueError("Invalid memory identifier")
    return value


class ContextMemory:
    """Synchronous SQLite API; async consumers should use ``asyncio.to_thread``.

    An optional local embedding callable must expose a stable ``name`` attribute.
    It is supplied by the caller; this class never installs or downloads a model.
    """

    def __init__(
        self, project_root: str | Path, *, embedding_backend: Callable[[str], list[float]] | None = None,
        max_file_bytes: int = 262144, max_index_files: int = 500,
    ):
        self.project_root = Path(project_root).resolve()
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.project_root / ".local-coder" / "context" / "memory.sqlite"
        self.max_file_bytes = max(1, min(int(max_file_bytes), 1048576))
        self.max_index_files = max(1, min(int(max_index_files), 5000))
        self.embedding_backend = embedding_backend
        self.embedding_name = "lexical-hash-v1" if embedding_backend is None else getattr(embedding_backend, "name", "")
        if not isinstance(self.embedding_name, str) or not self.embedding_name or _clean(self.embedding_name) != self.embedding_name:
            raise ValueError("Local embedding backend must have a non-secret stable name")
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.db_path), threading.RLock())
        with self._lock:
            self._check_storage(create=True)
            state_path(self.project_root, ".local-coder/context/memory.sqlite")
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
                        source_path TEXT, source_hash TEXT, content_hash TEXT NOT NULL, head TEXT,
                        task_id TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS terms (
                        memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                        term TEXT NOT NULL, frequency INTEGER NOT NULL, PRIMARY KEY(memory_id, term)
                    );
                    CREATE INDEX IF NOT EXISTS terms_lookup ON terms(term);
                    CREATE INDEX IF NOT EXISTS source_lookup ON memories(source_path, kind);
                    CREATE INDEX IF NOT EXISTS task_lookup ON memories(task_id);
                    CREATE TABLE IF NOT EXISTS vectors (
                        memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                        backend TEXT NOT NULL, value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS links (
                        source_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                        target_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                        relation TEXT NOT NULL, PRIMARY KEY(source_id, target_id, relation)
                    );
                    CREATE TABLE IF NOT EXISTS task_states (
                        task_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                """)
                owner = connection.execute("SELECT value FROM settings WHERE key='project_root'").fetchone()
                if owner and owner[0] != str(self.project_root):
                    raise ValueError("Memory database belongs to another project")
                connection.execute("INSERT OR IGNORE INTO settings VALUES ('project_root', ?)", (str(self.project_root),))

    def _check_storage(self, *, create: bool = False) -> None:
        for directory in (self.db_path.parent.parent, self.db_path.parent):
            if directory.is_symlink() or not directory.resolve().is_relative_to(self.project_root):
                raise ValueError("Memory storage path escapes project or uses a symlink")
            if create:
                directory.mkdir(exist_ok=True)
            if directory == self.db_path.parent and directory.exists():
                directory.chmod(0o700)
        for path in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
                raise ValueError("Memory database files must not be links")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._check_storage()
            if not self.db_path.exists():
                try:
                    descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    self._check_storage()
                else:
                    os.close(descriptor)
            self.db_path.chmod(0o600)
            connection = sqlite3.connect(self.db_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def _path(self, source_path: str | Path) -> Path:
        path = (self.project_root / source_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("Memory source path escapes project")
        return path

    def _head(self) -> str | None:
        try:
            result = subprocess.run(["git", "-C", str(self.project_root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _vector(self, content: str) -> list[float]:
        if self.embedding_backend is not None:
            vector = [float(value) for value in self.embedding_backend(content)]
            if not vector or len(vector) > 4096 or not all(math.isfinite(value) for value in vector):
                raise ValueError("Local embedding backend returned invalid vector")
        else:
            vector = [0.0] * 256
            for token in tokenize(content):
                features = [token] + [token[n:n + 3] for n in range(max(0, len(token) - 2))]
                for feature in features:
                    value = hashlib.blake2b(feature.encode(), digest_size=4).digest()
                    vector[int.from_bytes(value[:2], "big") % 256] += 1 if value[2] & 1 else -1
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    @staticmethod
    def _chunks(content: str, size: int = 1800) -> list[tuple[int, int, str]]:
        chunks, current, start, used = [], [], 1, 0
        for number, line in enumerate(content.splitlines(keepends=True), 1):
            if current and used + len(line) > size:
                chunks.append((start, number - 1, "".join(current)))
                current, start, used = [], number, 0
            # Long single lines are also bounded.
            if len(line) > size:
                for offset in range(0, len(line), size):
                    chunks.append((number, number, line[offset:offset + size]))
                start = number + 1
            else:
                current.append(line)
                used += len(line)
        if current:
            chunks.append((start, max(start, len(content.splitlines())), "".join(current)))
        return chunks[:128]

    def _prepare(self, kind: str, content: str, source_path: str | None, task_id: str | None, metadata: dict | None) -> list[dict]:
        if kind not in _KINDS:
            raise ValueError("Unsupported memory kind")
        if not isinstance(content, str) or len(content) > 200000:
            raise ValueError("Memory content must be text of at most 200000 characters")
        _identifier(task_id)
        path = self._path(source_path) if source_path else None
        relative = str(path.relative_to(self.project_root)) if path else None
        if relative and _clean(relative) != relative:
            raise ValueError("Memory source path contains a credential")
        source_hash = None
        if path and path.is_file():
            if path.stat().st_size > self.max_file_bytes:
                raise ValueError("Memory source exceeds file size limit")
            source_hash = _digest(path.read_bytes())
        cleaned = _clean(content)
        clean_metadata = _clean(metadata or {})
        if not isinstance(clean_metadata, dict) or len(json.dumps(clean_metadata)) > 16000:
            raise ValueError("Memory metadata must be an object of at most 16000 characters")
        links = clean_metadata.pop("links", [])
        if not isinstance(links, list) or len(links) > 100 or not all(isinstance(link, str) for link in links):
            raise ValueError("Memory links must be a list of memory IDs")
        head, timestamp = self._head(), _now()
        rows = []
        spans = self._chunks(cleaned)
        if kind == "code" and relative and relative.endswith(".py"):
            try:
                tree = ast.parse(cleaned)
                lines = cleaned.splitlines(keepends=True)
                # A top-level definition remains coherent where it fits; large ones
                # retain line references through the ordinary bounded window splitter.
                definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                if definitions:
                    spans = []
                    cursor = 1
                    for node in definitions:
                        begin = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
                        end = node.end_lineno or node.lineno
                        for offset, last in ((cursor, begin - 1), (begin, end)):
                            block = "".join(lines[offset - 1:last])
                            spans.extend((offset + first - 1, offset + final - 1, text) for first, final, text in self._chunks(block))
                        cursor = end + 1
                    spans.extend((cursor + first - 1, cursor + last - 1, text) for first, last, text in self._chunks("".join(lines[cursor - 1:])))
                    spans = spans[:128]
            except (SyntaxError, ValueError, RecursionError):
                pass
        for first, last, text in spans:
            if not text.strip():
                continue
            details = {**clean_metadata, "start_line": first, "end_line": last}
            rows.append({"id": uuid.uuid4().hex, "kind": kind, "content": text, "source_path": relative,
                         "source_hash": source_hash, "content_hash": _digest(text.encode()), "head": head,
                         "task_id": task_id, "metadata": details, "created_at": timestamp, "updated_at": timestamp,
                         "_links": links, "_vector": self._vector(text), "_terms": Counter(tokenize(text + " " + (relative or "")))})
        return rows

    def _insert(self, connection: sqlite3.Connection, rows: list[dict]) -> None:
        for row in rows:
            columns = ("id", "kind", "content", "source_path", "source_hash", "content_hash", "head", "task_id", "metadata", "created_at", "updated_at")
            connection.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(json.dumps(row[key]) if key == "metadata" else row[key] for key in columns))
            self._indices(connection, row)
            for target in row["_links"]:
                if connection.execute("SELECT 1 FROM memories WHERE id=?", (target,)).fetchone():
                    connection.execute("INSERT OR IGNORE INTO links VALUES (?,?,?)", (row["id"], target, "references"))
            previous = connection.execute("SELECT id FROM memories WHERE id<>? AND ((task_id IS NOT NULL AND task_id=?) OR (source_path IS NOT NULL AND source_path=?)) ORDER BY created_at DESC LIMIT 1", (row["id"], row["task_id"], row["source_path"])).fetchone()
            if previous:
                connection.execute("INSERT OR IGNORE INTO links VALUES (?,?,?)", (row["id"], previous[0], "related"))

    def _indices(self, connection: sqlite3.Connection, row: dict) -> None:
        connection.executemany("INSERT INTO terms VALUES (?,?,?)", [(row["id"], term, count) for term, count in row["_terms"].items()])
        connection.execute("INSERT INTO vectors VALUES (?,?,?)", (row["id"], self.embedding_name, json.dumps(row["_vector"])))

    def ingest(self, kind: str, content: str, source_path: str | None = None, task_id: str | None = None, metadata: dict | None = None) -> list[dict]:
        rows = self._prepare(kind, content, source_path, task_id, metadata)
        with self._connection() as connection:
            self._insert(connection, rows)
        return [self.get(row["id"]) for row in rows]

    def _freshness(self, row: dict, head: str | None) -> str:
        if row["source_path"]:
            try:
                path = self._path(row["source_path"])
                if not path.is_file():
                    return "source_missing"
                if path.stat().st_size > self.max_file_bytes or _digest(path.read_bytes()) != row["source_hash"]:
                    return "source_changed"
            except (OSError, ValueError):
                return "source_missing"
        if row["head"] != head:
            return "head_changed"
        return "fresh"

    def _view(self, connection: sqlite3.Connection, record: sqlite3.Row, head: str | None) -> dict:
        row = dict(record)
        row["metadata"] = json.loads(row["metadata"])
        row["freshness"] = self._freshness(row, head)
        stored = connection.execute("SELECT backend FROM vectors WHERE memory_id=?", (row["id"],)).fetchone()
        row["embedding_backend"] = stored[0] if stored else None
        row["untrusted"] = True
        line = row["metadata"].get("start_line", 1)
        row["source_ref"] = f"{row['source_path']}:{line}" if row["source_path"] else f"memory:{row['id']}"
        row["links"] = [item[0] for item in connection.execute("SELECT target_id FROM links WHERE source_id=? UNION SELECT source_id FROM links WHERE target_id=? LIMIT 50", (row["id"], row["id"]))]
        return row

    def get(self, memory_id: str) -> dict | None:
        head = self._head()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            return self._view(connection, row, head) if row else None

    def update(self, memory_id: str, content: str) -> dict | None:
        if not isinstance(content, str) or len(content) > 200000:
            raise ValueError("Memory content must be text of at most 200000 characters")
        cleaned = _clean(content)
        derived = {"id": memory_id, "_vector": self._vector(cleaned)}
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            original = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if original is None:
                return None
            derived["_terms"] = Counter(tokenize(cleaned + " " + (original["source_path"] or "")))
            connection.execute("UPDATE memories SET content=?, content_hash=?, updated_at=? WHERE id=?", (cleaned, _digest(cleaned.encode()), _now(), memory_id))
            connection.execute("DELETE FROM terms WHERE memory_id=?", (memory_id,))
            connection.execute("DELETE FROM vectors WHERE memory_id=?", (memory_id,))
            connection.execute("DELETE FROM links WHERE source_id=? OR target_id=?", (memory_id, memory_id))
            self._indices(connection, derived)
        return self.get(memory_id)

    def delete(self, memory_id: str) -> bool:
        with self._connection() as connection:
            return connection.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount > 0

    def list_memories(self, limit: int = 50) -> list[dict]:
        head = self._head()
        with self._connection() as connection:
            return [self._view(connection, row, head) for row in connection.execute("SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (max(0, min(int(limit), 200)),))]

    def search(self, query: str, limit: int = 8, char_budget: int = 6000, task_id: str | None = None) -> list[dict]:
        _identifier(task_id)
        budget, limit = max(0, min(int(char_budget), 50000)), max(0, min(int(limit), 50))
        query = _clean(str(query))[:4000]
        terms = list(dict.fromkeys(tokenize(query)))[:64]
        if not terms or budget == 0 or limit == 0 or query == "[REDACTED]":
            return []
        vector, head = self._vector(query), self._head()
        placeholders = ",".join("?" for _ in terms)
        with self._connection() as connection:
            # Exact term postings bound the lexical candidate set. Vectors rerank
            # lexical candidates; a supplied neural backend may add vector-only hits.
            records = connection.execute(f"SELECT m.*, v.value AS vector, v.backend AS vector_backend, count(t.term) AS lexical FROM memories m JOIN terms t ON t.memory_id=m.id JOIN vectors v ON v.memory_id=m.id WHERE t.term IN ({placeholders}) AND (? IS NULL OR m.task_id=? OR m.task_id IS NULL) GROUP BY m.id ORDER BY lexical DESC, m.updated_at DESC LIMIT 1000", (*terms, task_id, task_id)).fetchall()
            if self.embedding_backend is not None:
                additional = connection.execute("SELECT m.*, v.value AS vector, v.backend AS vector_backend, 0 AS lexical FROM memories m JOIN vectors v ON v.memory_id=m.id WHERE v.backend=? AND (? IS NULL OR m.task_id=? OR m.task_id IS NULL) ORDER BY m.updated_at DESC LIMIT 1000", (self.embedding_name, task_id, task_id)).fetchall()
                seen = {record["id"] for record in records}
                records = list(records) + [record for record in additional if record["id"] not in seen]
            ranked = []
            for record in records:
                row = self._view(connection, record, head)
                stored_vector = json.loads(row.pop("vector"))
                backend = row.pop("vector_backend")
                lexical = row.pop("lexical")
                similarity = sum(a * b for a, b in zip(vector, stored_vector)) if backend == self.embedding_name and len(vector) == len(stored_vector) else 0
                if row["kind"] == "code" and row["freshness"] != "fresh":
                    continue
                if not lexical and similarity < 0.65:
                    continue
                exact = query.casefold() in row["content"].casefold()
                row["score"] = round(lexical / len(terms) * 3 + max(0, similarity) + (2 if exact else 0), 6)
                row["reasons"] = ([f"{lexical} matching lexical terms"] if lexical else []) + [f"{backend} vector similarity {similarity:.3f}"]
                if exact:
                    row["reasons"].append("exact phrase")
                if row["freshness"] != "fresh":
                    row["reasons"].append("historical evidence: " + row["freshness"])
                ranked.append(row)
            ranked.sort(key=lambda row: (row["score"], row["updated_at"], row["id"]), reverse=True)
            hits = []
            for row in ranked[:limit]:
                if budget <= 0:
                    break
                row["truncated"] = len(row["content"]) > budget
                row["content"] = row["content"][:budget]
                budget -= len(row["content"])
                hits.append(row)
            return hits

    def task_state(self, task_id: str, updates: dict | None = None) -> dict:
        if not task_id:
            raise ValueError("Task ID is required")
        _identifier(task_id)
        if updates is not None and (not isinstance(updates, dict) or set(updates) - _STATE_FIELDS):
            raise ValueError("Unknown task state fields")
        cleaned = _clean(updates or {})
        for key, value in cleaned.items():
            if key == "objective" and not isinstance(value, str):
                raise ValueError("Task objective must be text")
            if key != "objective" and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
                raise ValueError("Task state collections must be lists of text")
        if len(json.dumps(cleaned)) > 32000:
            raise ValueError("Task state updates exceed 32000 characters")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM task_states WHERE task_id=?", (task_id,)).fetchone()
            state = json.loads(row[0]) if row else {key: "" if key == "objective" else [] for key in sorted(_STATE_FIELDS)}
            state.update(cleaned)
            if len(json.dumps(state)) > 32000:
                raise ValueError("Task state exceeds 32000 characters")
            if updates is not None:
                connection.execute("INSERT INTO task_states VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at", (task_id, json.dumps(state), _now()))
            return {"task_id": task_id, **state}

    def context(self, query: str, char_budget: int = 4000, task_id: str | None = None, *, limit: int = 8) -> dict:
        budget = max(0, min(int(char_budget), 50000))
        items = self.search(query, char_budget=budget, task_id=task_id, limit=limit)
        text = "Untrusted memory evidence; verify sources before acting.\n"[:budget] if items else ""
        included = []
        for item in items:
            header = f"\n[{item['id']}] {item['source_ref']} ({item['freshness']})\n"
            remaining = budget - len(text) - len(header)
            if remaining <= 0:
                break
            content = item["content"][:remaining]
            text += header + content
            included.append({**item, "content": content, "truncated": item["truncated"] or len(content) < len(item["content"])})
        return {"text": text, "items": included, "untrusted": True, "embedding_backend": self.embedding_name, "char_budget": budget}

    @staticmethod
    def _indexable(path: Path, root: Path) -> bool:
        relative = path.relative_to(root)
        name = path.name.lower()
        return (not any(part in SKIP_DIRS or part.startswith(".env") for part in relative.parts)
                and name not in _PRIVATE_NAMES and path.suffix.lower() not in SKIP_EXTENSIONS | _PRIVATE_SUFFIXES
                and not name.startswith(("secrets.", "credentials.")))

    def index_code(self, paths: list[str] | None = None) -> dict:
        if paths is not None and (not isinstance(paths, list) or len(paths) > self.max_index_files):
            raise ValueError("Index paths must be a bounded list")
        roots = [self._path(path) for path in paths] if paths is not None else [self.project_root]
        candidates: list[Path] = []
        for root in roots:
            if root.is_file():
                candidates.append(root)
            elif root.is_dir():
                for directory, subdirs, files in os.walk(root, followlinks=False):
                    subdirs[:] = sorted(name for name in subdirs if name not in SKIP_DIRS and not (Path(directory) / name).is_symlink() and not name.startswith(".env"))
                    candidates.extend(Path(directory) / name for name in sorted(files))
                    if len(candidates) >= self.max_index_files * 5:
                        break
        prepared, indexed_paths, skipped = [], set(), 0
        for path in sorted(set(candidates)):
            if len(indexed_paths) >= self.max_index_files:
                break
            if path.is_symlink() or not self._indexable(path, self.project_root):
                skipped += 1
                continue
            try:
                path = self._path(path)
                if path.stat().st_size > self.max_file_bytes:
                    skipped += 1
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw:
                    skipped += 1
                    continue
                content = raw.decode("utf-8")
                relative = str(path.relative_to(self.project_root))
                rows = self._prepare("code", content, relative, None, {"indexed": True})
                for row in rows:
                    row["source_hash"] = _digest(raw)
                prepared.extend(rows)
                indexed_paths.add(relative)
            except (OSError, UnicodeError, ValueError):
                skipped += 1
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for relative in indexed_paths:
                connection.execute("DELETE FROM memories WHERE kind='code' AND source_path=?", (relative,))
            # Only remove missing files in the requested scope. File changes are
            # independently invalidated at retrieval time, including partial runs.
            for row in connection.execute("SELECT id, source_path FROM memories WHERE kind='code'").fetchall():
                source = self._path(row["source_path"]) if row["source_path"] else None
                if source and not source.exists() and any(source == root or source.is_relative_to(root) for root in roots):
                    connection.execute("DELETE FROM memories WHERE id=?", (row["id"],))
            self._insert(connection, prepared)
        return {"files": len(indexed_paths), "chunks": len(prepared), "skipped": skipped, "max_files": self.max_index_files, "embedding_backend": self.embedding_name}

    def status(self) -> dict:
        with self._connection() as connection:
            return {"memories": connection.execute("SELECT count(*) FROM memories").fetchone()[0],
                    "tasks": connection.execute("SELECT count(*) FROM task_states").fetchone()[0],
                    "links": connection.execute("SELECT count(*) FROM links").fetchone()[0],
                    "embedding_backend": self.embedding_name,
                    "embedding_kind": "supplied_local_backend" if self.embedding_backend is not None else "lexical_subword_hash",
                    "neural_embeddings": bool(getattr(self.embedding_backend, "is_neural", False)),
                    "project_root": str(self.project_root), "database": str(self.db_path), "untrusted": True}

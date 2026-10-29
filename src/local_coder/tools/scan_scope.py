"""Bounded source enumeration shared by investigation tools."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from contextlib import contextmanager

from local_coder.context.source_snapshot import SourceSnapshotError, read_source_snapshot
from local_coder.workspace import Workspace

IGNORED = {"upstream", ".build", ".build-venv", ".cache", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules", "__pycache__", ".local-coder", "build", "dist", "release"}
MAX_SCAN_FILES = 1000
MAX_SCAN_BYTES = 20_000_000
MAX_SCAN_FILE_BYTES = 1_000_000
MAX_SCAN_ENTRIES = 20000
MAX_SCAN_DIRECTORIES = 1000
MAX_SCAN_DEPTH = 50
MAX_SKIP_RECORDS = 100


def record_scan_skip(coverage: dict, path: str | Path, reason: str) -> None:
    """Retain bounded coverage diagnostics without pretending skipped input passed."""
    skipped = coverage.setdefault("skipped", [])
    coverage["skipped_count"] = coverage.get("skipped_count", len(skipped)) + 1
    if len(skipped) < MAX_SKIP_RECORDS:
        skipped.append(f"{str(path)[:256]}: {reason[:256]}")
    else:
        coverage["skipped_omitted"] = coverage.get("skipped_omitted", 0) + 1
    coverage["status"] = "partial"


def read_scan_source(root: str | Path, path: str | Path, *, coverage: dict | None = None,
                     errors: str = "strict") -> str:
    """Read bounded regular source bytes; scanners may inspect credential fixtures.

    Unlike automatic context retrieval, this helper does not filter credential
    paths. No-follow snapshots reject special files, symlinks and read races.
    Passing coverage charges successful bytes and conservatively reserves the
    allowance for interrupted/failed reads. Repeated reads are charged again.
    """
    limit = MAX_SCAN_FILE_BYTES
    used = successful = reserved = 0
    if coverage is not None:
        successful = coverage.get("bytes_read", 0)
        reserved = coverage.get("read_bytes_reserved", 0)
        if any(type(value) is not int or value < 0 for value in (successful, reserved)):
            raise SourceSnapshotError("Invalid scan read budget")
        used = successful + reserved
        limit = min(limit, MAX_SCAN_BYTES - used)
        if limit <= 0:
            raise SourceSnapshotError("Aggregate scan read budget reached")
    try:
        snapshot = read_source_snapshot(root, path, max_bytes=limit)
    except (OSError, SourceSnapshotError):
        if coverage is not None:
            coverage["read_bytes_reserved"] = reserved + limit
            coverage["read_budget_used"] = used + limit
        raise
    if coverage is not None:
        coverage["bytes_read"] = successful + snapshot.size
        coverage["read_budget_used"] = used + snapshot.size
    return snapshot.data.decode("utf-8", errors=errors)


@contextmanager
def _directory_entries(root: Path, directory: Path):
    """Open directory components without following links where supported."""
    relative = directory.relative_to(root)
    descriptors = []
    try:
        if (hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
                and os.scandir in os.supports_fd):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptors.append(os.open(root, flags))
            for part in relative.parts:
                descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
            with os.scandir(descriptors[-1]) as entries:
                yield entries
        else:
            # Portable validation cannot provide the POSIX descriptor guarantee.
            if not directory.resolve().is_relative_to(root) or any(
                    root.joinpath(*relative.parts[:index]).is_symlink()
                    for index in range(1, len(relative.parts) + 1)):
                raise ValueError("Directory links are not scanned")
            with os.scandir(directory) as entries:
                yield entries
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def scan_files(root: Path, target: Path) -> tuple[list[Path], dict]:
    workspace = Workspace(root)
    root = workspace.root
    target = Path(os.path.abspath(root / target))
    files = []
    coverage = {"files_considered": 0, "bytes": 0, "bytes_read": 0, "skipped": [],
                "status": "complete", "entries_considered": 0, "directories_considered": 0,
                "limits": {"files": MAX_SCAN_FILES, "bytes": MAX_SCAN_BYTES,
                           "file_bytes": MAX_SCAN_FILE_BYTES, "entries": MAX_SCAN_ENTRIES,
                           "directories": MAX_SCAN_DIRECTORIES, "depth": MAX_SCAN_DEPTH}}

    def consider(path, info):
        relative = path.relative_to(root)
        if not stat.S_ISREG(info.st_mode):
            record_scan_skip(coverage, "source" if stat.S_ISLNK(info.st_mode) else relative,
                             "Non-regular file or symbolic link skipped")
        elif info.st_size > MAX_SCAN_FILE_BYTES:
            record_scan_skip(coverage, relative, "Exceeds 1 MB scan limit")
        elif coverage["bytes"] + info.st_size > MAX_SCAN_BYTES:
            record_scan_skip(coverage, relative, "Aggregate scan byte budget reached")
            return False
        else:
            files.append(path)
            coverage["bytes"] += info.st_size
        return True

    try:
        workspace.resolve(target)
        info = target.lstat()
        if not stat.S_ISDIR(info.st_mode):
            consider(target, info)
            coverage["files_considered"] = len(files)
            return files, coverage
    except (OSError, ValueError):
        record_scan_skip(coverage, "target", "Unreadable or out-of-workspace target skipped")
        return files, coverage

    pending = [(target, 0)]
    exhausted = False
    while pending and not exhausted:
        directory, depth = pending.pop()
        if coverage["directories_considered"] >= MAX_SCAN_DIRECTORIES:
            record_scan_skip(coverage, ".", "Directory budget reached; narrow the target")
            break
        coverage["directories_considered"] += 1
        try:
            with _directory_entries(root, directory) as entries:
                for entry in entries:
                    if (len(files) >= MAX_SCAN_FILES or coverage["bytes"] >= MAX_SCAN_BYTES
                            or coverage["entries_considered"] >= MAX_SCAN_ENTRIES):
                        record_scan_skip(coverage, ".", "Scan budget reached; narrow the target to inspect remaining files")
                        exhausted = True
                        break
                    coverage["entries_considered"] += 1
                    path = directory / entry.name
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            if entry.name in IGNORED:
                                continue
                            if depth >= MAX_SCAN_DEPTH:
                                record_scan_skip(coverage, path.relative_to(root), "Directory depth limit reached")
                            elif len(pending) + coverage["directories_considered"] >= MAX_SCAN_DIRECTORIES:
                                record_scan_skip(coverage, path.relative_to(root), "Directory budget reached; narrow the target")
                            else:
                                pending.append((path, depth + 1))
                        elif not consider(path, info):
                            exhausted = True
                            break
                    except (OSError, ValueError):
                        record_scan_skip(coverage, "source", "Unreadable or changed file skipped")
        except (OSError, ValueError):
            record_scan_skip(coverage, "directory", "Unreadable, changed or linked directory skipped")
    files.sort()
    coverage["files_considered"] = len(files)
    return files, coverage

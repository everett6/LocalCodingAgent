"""Bounded source enumeration shared by investigation tools."""
from __future__ import annotations

import os
from pathlib import Path

from local_coder.workspace import Workspace

IGNORED = {"upstream", ".build", ".build-venv", ".cache", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules", "__pycache__", ".local-coder", "build", "dist", "release"}


def scan_files(root: Path, target: Path) -> tuple[list[Path], dict]:
    workspace = Workspace(root)
    files, skipped = [], []
    total = 0
    def candidates():
        if target.is_file():
            yield target
            return
        for directory, dirs, names in os.walk(target, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED and not (Path(directory) / d).is_symlink())
            for name in sorted(names):
                yield Path(directory) / name
    for path in candidates():
        if len(files) >= 1000 or total >= 20_000_000:
            skipped.append("Scan budget reached; narrow the target to inspect remaining files")
            break
        try:
            workspace.resolve(path)
            if path.is_symlink():
                skipped.append("Symbolic link skipped")
                continue
            size = path.stat().st_size
            if size > 1_000_000:
                skipped.append(f"{path.relative_to(root)}: exceeds 1 MB scan limit")
                continue
            files.append(path)
            total += size
        except (OSError, ValueError):
            skipped.append("Unreadable or out-of-workspace file skipped")
    return files, {"files_considered": len(files), "bytes": total, "skipped": skipped,
                   "status": "partial" if skipped else "complete"}

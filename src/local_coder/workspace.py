"""Workspace boundary checks for files and working directories."""
from __future__ import annotations

import os
from pathlib import Path


class Workspace:
    """Resolve paths without allowing escapes from a project workspace."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()

    def resolve(self, path: str | os.PathLike[str] = ".") -> Path:
        """Resolve a path and reject traversal, including symlink escapes."""
        candidate = self.root / Path(path)
        resolved = Path(os.path.realpath(candidate))
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path escapes workspace: {path}")
        return resolved

    def relative_path(self, path: str | os.PathLike[str]) -> str:
        """Return a workspace-relative path after validating it."""
        return str(self.resolve(path).relative_to(self.root))
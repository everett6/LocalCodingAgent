"""Safe writes into the project's .local-coder state directory.

The state directory lives inside the user's checkout, which may be an
untrusted repository. A repo that commits `.local-coder` (or a file in it)
as a symlink must not be able to redirect our writes outside the workspace,
and files we write there (tool output, the code index) can hold secrets, so
the directory ignores itself in git.
"""
from __future__ import annotations

import os
from pathlib import Path

STATE_DIR = ".local-coder"


def state_path(project_root: str | os.PathLike, relpath: str | os.PathLike) -> Path:
    """Return project_root/relpath with its parent directories created.

    Raises ValueError if the path, or any directory on the way to it, is a
    symlink or would land outside the workspace.
    """
    root = Path(os.path.realpath(project_root))
    rel = Path(relpath)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Invalid state path: {relpath}")
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing to write through symlink: {current}")
        current.mkdir(exist_ok=True)
        if rel.parts[0] == STATE_DIR and current == root / STATE_DIR:
            _ignore_in_git(current)
    target = current / rel.parts[-1]
    if target.is_symlink():
        raise ValueError(f"Refusing to write through symlink: {target}")
    return target


def write_state_file(project_root: str | os.PathLike, relpath: str | os.PathLike, text: str) -> Path:
    """Write text to a state file without following symlinks."""
    target = state_path(project_root, relpath)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return target


def _ignore_in_git(state_dir: Path) -> None:
    gitignore = state_dir / ".gitignore"
    if gitignore.exists() or gitignore.is_symlink():
        return
    try:
        fd = os.open(gitignore, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except OSError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Written by local-coder: session state, tool output and caches.\n*\n")

"""Bounded source snapshots with hashes tied to the bytes actually read."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat


class SourceSnapshotError(ValueError):
    """Source is unsafe, unsupported, too large, or changed during the read."""


@dataclass(frozen=True)
class SourceSnapshot:
    data: bytes
    sha256: str
    size: int
    mtime_ns: int


def is_private_source(path: str | Path) -> bool:
    """Exclude common local credentials and engine state from automatic retrieval."""
    parts = Path(path).parts
    private_dirs = {".git", ".local-coder", ".ssh", ".aws", ".azure", ".kube", ".docker", ".codex", ".claude"}
    private_names = {".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
    return any(part.lower().startswith(".env") or part.lower() in private_dirs for part in parts) or bool(parts and parts[-1].lower() in private_names)


def read_source_snapshot(project_root: str | Path, path: str | Path, *, max_bytes: int = 512 * 1024) -> SourceSnapshot:
    """Read one regular workspace file, bounded before and during the read.

    POSIX no-follow directory descriptors confine every path component. Other
    platforms validate resolved paths before and after the bounded descriptor
    read; this portable fallback is not an OS isolation boundary.
    """
    if type(max_bytes) is not int or not 0 < max_bytes <= 64 * 1024 * 1024:
        raise ValueError("Source byte limit must be between 1 and 64 MiB")
    root = Path(project_root).resolve()
    candidate = Path(os.path.abspath(root / path))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise SourceSnapshotError("Source path escapes workspace") from None
    if not relative.parts:
        raise SourceSnapshotError("Source must be a regular workspace file")
    descriptors = []
    try:
        if hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptors.append(os.open(root, directory_flags))
            for part in relative.parts[:-1]:
                descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
            descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=descriptors[-1])
        else:
            if not candidate.resolve().is_relative_to(root) or any((root.joinpath(*relative.parts[:index])).is_symlink()
                                                                  for index in range(1, len(relative.parts) + 1)):
                raise SourceSnapshotError("Source symlinks are not read")
            descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceSnapshotError("Source must be a regular workspace file")
        if before.st_size > max_bytes:
            raise SourceSnapshotError("Source exceeds the byte limit")
        data = bytearray()
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(data))):
            data.extend(chunk)
            if len(data) > max_bytes:
                raise SourceSnapshotError("Source exceeds the byte limit")
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in fields) or len(data) != after.st_size:
            raise SourceSnapshotError("Source changed while reading")
        # Reject a path replaced while its old inode was read as well.
        current = candidate.stat(follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or any(getattr(current, field) != getattr(after, field) for field in fields):
            raise SourceSnapshotError("Source path changed while reading")
        content = bytes(data)
        return SourceSnapshot(content, hashlib.sha256(content).hexdigest(), after.st_size, after.st_mtime_ns)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

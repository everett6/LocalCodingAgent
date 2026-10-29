"""Source hashes describe bounded bytes, never a later replacement."""
import hashlib
import os
from pathlib import Path

import pytest

from local_coder.context.source_snapshot import SourceSnapshotError, read_source_snapshot
from local_coder.core.runtime import workspace_file_fingerprints


def test_snapshot_bytes_and_hash_are_same_source(tmp_path):
    (tmp_path / "app.py").write_bytes(b"print('same bytes')\n")
    snapshot = read_source_snapshot(tmp_path, "app.py")
    assert snapshot.sha256 == hashlib.sha256(snapshot.data).hexdigest()
    assert snapshot.size == len(snapshot.data)


def test_oversized_source_is_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / "large.py"
    path.write_bytes(b"x" * 20)
    reads = []
    original = os.read

    def record(*args):
        reads.append(args)
        return original(*args)

    monkeypatch.setattr(os, "read", record)
    with pytest.raises(SourceSnapshotError, match="byte limit"):
        read_source_snapshot(tmp_path, "large.py", max_bytes=10)
    assert not reads


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special file")
def test_fifo_source_never_blocks_or_becomes_a_context_fingerprint(tmp_path):
    os.mkfifo(tmp_path / "pipe.py")
    with pytest.raises(SourceSnapshotError, match="regular"):
        read_source_snapshot(tmp_path, "pipe.py")
    with pytest.raises(SourceSnapshotError, match="regular"):
        workspace_file_fingerprints(str(tmp_path), ["pipe.py"])


@pytest.mark.parametrize("component", ["file", "directory"])
def test_symlink_components_do_not_read_outside_source(tmp_path, component):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir(); outside.mkdir()
    (outside / "app.py").write_text("OUTSIDE_CANARY")
    if component == "file":
        (root / "app.py").symlink_to(outside / "app.py")
        path = "app.py"
    else:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        path = "linked/app.py"
    with pytest.raises((OSError, SourceSnapshotError)):
        read_source_snapshot(root, path)


def test_replacement_during_read_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "app.py"
    path.write_text("old source\n")
    original = os.read
    replaced = False

    def replace_on_read(*args):
        nonlocal replaced
        data = original(*args)
        if not replaced:
            replacement = tmp_path / "replacement.py"
            replacement.write_text("new source\n")
            replacement.replace(path)
            replaced = True
        return data

    monkeypatch.setattr(os, "read", replace_on_read)
    with pytest.raises(SourceSnapshotError, match="changed while reading"):
        read_source_snapshot(tmp_path, "app.py")


def test_growth_during_read_cannot_exceed_cap(tmp_path, monkeypatch):
    path = tmp_path / "app.py"
    path.write_bytes(b"small")
    original = os.read
    changed = False

    def grow_before_read(*args):
        nonlocal changed
        if not changed:
            path.write_bytes(b"x" * 1000)
            changed = True
        return original(*args)

    monkeypatch.setattr(os, "read", grow_before_read)
    with pytest.raises(SourceSnapshotError, match="byte limit"):
        read_source_snapshot(tmp_path, "app.py", max_bytes=10)

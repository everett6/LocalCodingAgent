"""Tests for Git-backed workspace checkpoints."""
import subprocess

from local_coder.git import CheckpointManager


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )


def test_checkpoint_restores_tracked_and_untracked_changes(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "user.email", "test@example.com")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before\n")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-qm", "initial")

    tracked.write_text("checkpoint version\n")
    untracked = tmp_path / "new.txt"
    untracked.write_text("new file\n")
    manager = CheckpointManager(str(tmp_path))
    checkpoint = manager.create()

    tracked.write_text("later version\n")
    untracked.write_text("later new file\n")
    (tmp_path / "extra.txt").write_text("remove me\n")

    manager.rollback(checkpoint.checkpoint_id)

    assert tracked.read_text() == "checkpoint version\n"
    assert untracked.read_text() == "new file\n"
    assert not (tmp_path / "extra.txt").exists()


def test_checkpoint_listing_is_newest_first(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "file.txt").write_text("content\n")
    git(tmp_path, "add", "file.txt")
    git(tmp_path, "commit", "-qm", "initial")

    manager = CheckpointManager(str(tmp_path))
    first = manager.create()
    second = manager.create()

    listed = manager.list()

    assert {item.checkpoint_id for item in listed} == {first.checkpoint_id, second.checkpoint_id}
    assert listed[0].created_at >= listed[1].created_at
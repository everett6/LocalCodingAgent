import subprocess
import sys
import threading
from types import SimpleNamespace

from local_coder.workbench import WorkbenchAPI, workspace_fingerprint
from local_coder.git.checkpoints import CheckpointManager


class UndoAPI(WorkbenchAPI):
    def __init__(self, project_root):
        self.project_root = str(project_root)
        self._job_lock = threading.Lock()
        self.sessions = SessionRecords()


class SessionRecords(dict):
    def save(self, session_id, **data):
        self.setdefault(session_id, {}).update(data)


def _session(checkpoint, root, status="completed"):
    return {
        "status": status,
        "pre_run_checkpoint_id": checkpoint.checkpoint_id,
        "workspace_fingerprint": workspace_fingerprint(str(root)),
    }


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    target = tmp_path / "app.py"
    target.write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "initial")
    return target


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )


def test_undo_restores_latest_checkpoint(tmp_path):
    target = _repo(tmp_path)
    checkpoint = CheckpointManager(str(tmp_path)).create()
    target.write_text("after\n", encoding="utf-8")
    api = UndoAPI(tmp_path)
    api.sessions["session-1"] = _session(checkpoint, tmp_path)

    status, payload = api.workbench_handle("POST", "/undo", {"session_id": "session-1"})

    assert status == 200
    assert payload["checkpoint_id"] == checkpoint.checkpoint_id
    assert target.read_text(encoding="utf-8") == "before\n"


def test_undo_rejects_an_active_job(tmp_path):
    api = UndoAPI(tmp_path)
    assert api._job_lock.acquire(blocking=False)
    try:
        status, payload = api.workbench_handle("POST", "/undo", {"session_id": "session-1"})
    finally:
        api._job_lock.release()

    assert status == 409
    assert "active task" in payload["error"]


def test_undo_reports_when_no_checkpoint_exists(tmp_path):
    _repo(tmp_path)
    status, payload = UndoAPI(tmp_path).workbench_handle("POST", "/undo", {"session_id": "missing"})

    assert status == 409
    assert "finished workspace snapshot" in payload["error"]


def test_undo_refuses_to_discard_changes_made_after_task(tmp_path):
    target = _repo(tmp_path)
    checkpoint = CheckpointManager(str(tmp_path)).create()
    target.write_text("task result\n", encoding="utf-8")
    api = UndoAPI(tmp_path)
    api.sessions["session-1"] = _session(checkpoint, tmp_path)
    target.write_text("newer user edit\n", encoding="utf-8")

    status, payload = api.workbench_handle("POST", "/undo", {"session_id": "session-1"})

    assert status == 409
    assert "Workspace changed" in payload["error"]
    assert target.read_text(encoding="utf-8") == "newer user edit\n"


def test_undo_refuses_to_reset_a_commit_made_after_task(tmp_path):
    target = _repo(tmp_path)
    checkpoint = CheckpointManager(str(tmp_path)).create()
    target.write_text("task result\n", encoding="utf-8")
    api = UndoAPI(tmp_path)
    api.sessions["session-1"] = _session(checkpoint, tmp_path)
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "later user commit")

    status, payload = api.workbench_handle("POST", "/undo", {"session_id": "session-1"})

    assert status == 409
    assert "Workspace changed" in payload["error"]
    assert target.read_text(encoding="utf-8") == "task result\n"


def test_undo_refuses_when_a_newer_checkpoint_exists(tmp_path):
    target = _repo(tmp_path)
    checkpoint = CheckpointManager(str(tmp_path)).create()
    target.write_text("task result\n", encoding="utf-8")
    api = UndoAPI(tmp_path)
    api.sessions["session-1"] = _session(checkpoint, tmp_path)
    CheckpointManager(str(tmp_path)).create()
    api.sessions["session-1"]["workspace_fingerprint"] = workspace_fingerprint(str(tmp_path))

    status, payload = api.workbench_handle("POST", "/undo", {"session_id": "session-1"})

    assert status == 409
    assert "newer checkpoint" in payload["error"]
    assert target.read_text(encoding="utf-8") == "task result\n"


def test_file_diff_shows_untracked_file_content(tmp_path, monkeypatch):
    _repo(tmp_path)
    added = tmp_path / "new.py"
    added.write_text("print('new')\n", encoding="utf-8")

    def git(root, *args):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else ""

    monkeypatch.setitem(sys.modules, "local_coder.remote", SimpleNamespace(_git=git))
    status, payload = UndoAPI(tmp_path).workbench_handle("GET", "/diff?path=new.py", {})

    assert status == 200
    assert payload["path"] == "new.py"
    assert "+print('new')" in payload["diff"]

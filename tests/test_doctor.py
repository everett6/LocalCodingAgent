import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_coder import doctor
from local_coder.engine_discovery import canonical_workspace

IDE_DIAGNOSTIC = doctor.ide_diagnostic


def descriptor(tmp_path, monkeypatch, **changes):
    directory = tmp_path / "connections"
    directory.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("LOCAL_CODER_CONNECTION_DIR", str(directory))
    path = doctor.descriptor_path(tmp_path)
    body = {"workspace": canonical_workspace(tmp_path), "origin": "http://127.0.0.1:8234",
            "pid": 999999, "token": "credential-should-never-appear"}
    body.update(changes)
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return path


class Inference:
    def __init__(self, state="stopped", **settings):
        self.state = state
        self.settings = settings
        self.stops = 0

    def status(self):
        return {"state": self.state, "running": self.state == "running", "settings": self.settings,
                "error": "credential-should-never-appear"}

    def _runtime(self, value):
        if value:
            return Path(value)
        raise ValueError("credential-should-never-appear")

    def stop(self):
        self.stops += 1
        self.state = "stopped"


@pytest.fixture(autouse=True)
def local_checks(monkeypatch):
    monkeypatch.setattr(doctor, "ide_diagnostic", lambda: {"status": "unknown", "summary": "fixture"})


def test_diagnostics_only_return_boolean_provider_presence_and_safe_runtime_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_CONNECTION_DIR", str(tmp_path / "absent"))
    model = tmp_path / "selected.gguf"
    model.write_bytes(b"GGUF" + b"\0" * 20)
    runtime = tmp_path / "llama-server"
    runtime.write_bytes(b"fixture")
    monkeypatch.setattr(doctor, "inspect_runtime", lambda path: {"valid": True, "path": "credential-should-never-appear"})
    provider = SimpleNamespace(model_id="credential-should-never-appear", base_url="https://example.invalid/private-key", resolve_api_key=lambda: "credential-should-never-appear")
    discovery = SimpleNamespace(status=lambda: {"runtimes": [{"path": "private-runtime"}], "models": []})
    report = doctor.diagnose(tmp_path, Inference(model_path=str(model), runtime_path=str(runtime)), provider, discovery)
    assert report["checks"]["runtime"]["model_path_valid"]
    assert report["checks"]["runtime"]["executable_valid"]
    assert report["checks"]["provider"]["key_present"] is True
    assert report["checks"]["provider"]["model_selected"] is True
    assert "credential-should-never-appear" not in json.dumps(report)
    assert "private-runtime" not in json.dumps(report)
    assert report["network_probes"] is False


def test_failed_runtime_reset_is_verified_and_running_runtime_rejected(tmp_path):
    runtime = Inference("error")
    assert doctor.repair(tmp_path, "reset_failed_runtime", runtime)
    assert runtime.stops == 1
    for state in ("running", "starting", "stopped"):
        runtime.state = state
        with pytest.raises(ValueError, match="failed"):
            doctor.repair(tmp_path, "reset_failed_runtime", runtime)
    assert runtime.stops == 1


def test_descriptor_only_deleted_when_verified_dead_and_safe(tmp_path, monkeypatch):
    path = descriptor(tmp_path, monkeypatch)
    for state in ("alive", "unknown"):
        monkeypatch.setattr(doctor, "pid_state", lambda pid, state=state: state)
        assert doctor.engine_diagnostic(tmp_path)["port"] == 8234
        with pytest.raises(ValueError, match="active or unknown"):
            doctor.repair(tmp_path, "clear_stale_engine_descriptor")
        assert path.exists()
    monkeypatch.setattr(doctor, "pid_state", lambda pid: "dead")
    assert doctor.repair(tmp_path, "clear_stale_engine_descriptor")
    assert not path.exists()


@pytest.mark.parametrize("change", ["symlink", "hardlink", "public", "wrong-workspace", "external-origin", "oversized", "changed"])
def test_descriptor_rejects_unsafe_or_changed_private_files(tmp_path, monkeypatch, change):
    path = descriptor(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, "pid_state", lambda pid: "dead")
    if change == "symlink":
        secret = tmp_path / "secret"
        secret.write_text("unchanged")
        path.unlink()
        path.symlink_to(secret)
    elif change == "hardlink":
        os.link(path, tmp_path / "copy")
    elif change == "public":
        path.chmod(0o644)
    elif change == "wrong-workspace":
        descriptor(tmp_path, monkeypatch, workspace="other")
    elif change == "external-origin":
        descriptor(tmp_path, monkeypatch, origin="http://external.invalid:8234")
    elif change == "oversized":
        path.write_text("x" * 20000)
    else:
        def changed(pid):
            path.write_text("updated")
            return "dead"
        monkeypatch.setattr(doctor, "pid_state", changed)
    with pytest.raises((ValueError, OSError)):
        doctor.repair(tmp_path, "clear_stale_engine_descriptor")
    assert path.exists()
    if change == "symlink":
        assert secret.read_text() == "unchanged"


def test_project_state_repair_only_changes_owned_directory(tmp_path):
    folder = tmp_path / ".local-coder"
    folder.mkdir(mode=0o755)
    user_file = folder / "user.txt"
    user_file.write_text("preserve")
    user_file.chmod(0o644)
    assert doctor.state_diagnostic(tmp_path)["repairable"]
    assert doctor.repair(tmp_path, "ensure_private_project_state")
    assert folder.stat().st_mode & 0o777 == 0o700
    assert user_file.read_text() == "preserve" and user_file.stat().st_mode & 0o777 == 0o644
    with pytest.raises(ValueError):
        doctor.repair(tmp_path, "arbitrary-chmod")


def test_project_state_symlink_or_foreign_owner_not_repairable(tmp_path, monkeypatch):
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / ".local-coder").symlink_to(external, target_is_directory=True)
    assert doctor.state_diagnostic(tmp_path)["repairable"] is False
    with pytest.raises(ValueError):
        doctor.repair(tmp_path, "ensure_private_project_state")
    (tmp_path / ".local-coder").unlink()
    (tmp_path / ".local-coder").mkdir()
    monkeypatch.setattr(doctor, "_owned", lambda info: False)
    assert doctor.state_diagnostic(tmp_path)["repairable"] is False


def test_pid_permission_denial_is_unknown_not_dead(monkeypatch):
    def denied(*args):
        raise PermissionError()
    monkeypatch.setattr(os, "kill", denied)
    assert doctor.pid_state(123) == "unknown"


def test_ide_sandbox_checks_never_repair_administrator_permissions(tmp_path, monkeypatch):
    executable = tmp_path / "local-coder-ide"
    executable.write_bytes(b"fixture")
    executable.chmod(0o755)
    helper = tmp_path / "chrome-sandbox"
    helper.write_bytes(b"fixture")
    helper.chmod(0o755)
    monkeypatch.setenv("LOCAL_CODER_IDE_EXECUTABLE", str(executable))
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    assert IDE_DIAGNOSTIC()["sandbox"]["root_owned_4755"] is False
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_mode=0o104755, st_uid=0) if path == helper else original(path))
    assert IDE_DIAGNOSTIC()["sandbox"]["root_owned_4755"] is True
    assert helper.stat().st_mode & 0o7777 == 0o755
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    assert IDE_DIAGNOSTIC()["sandbox"]["status"] == "unsupported"

import json
import threading
from types import SimpleNamespace

import pytest

from local_coder.doctor_api import DoctorAPI
from local_coder.harness_api import HarnessAPI


class API(DoctorAPI, HarnessAPI):
    def __init__(self, root):
        self.project_root = str(root)
        self._job_lock = threading.Lock()
        self._harness_fallback_job_lock = self._job_lock
        self.active_job = None
        self.inference = SimpleNamespace(status=lambda: {"state": "stopped", "running": False, "settings": {}})

    def _selected_provider_config(self):
        return SimpleNamespace(model_id="private-id", base_url="private-url", resolve_api_key=lambda: "private-token")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_CONNECTION_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr("local_coder.doctor.ide_diagnostic", lambda: {"status": "unknown", "summary": "fixture"})
    return API(tmp_path)


def test_get_is_read_only_redacted_and_uses_tuple_semantics(api, tmp_path):
    handled, report = api._doctor_get("/doctor", {})
    assert handled
    assert not (tmp_path / ".local-coder").exists()
    assert "private-id" not in json.dumps(report) and "private-token" not in json.dumps(report)
    assert api._doctor_get("/other", {}) == (False, None)
    assert api._doctor_post("/other", {}) == (False, None)
    with pytest.raises(ValueError):
        api._doctor_get("/doctor", {"scan": ["1"]})


def test_repair_requires_idle_and_reports_verified_before_after(api, tmp_path):
    api.active_job = "running"
    with pytest.raises(ValueError, match="idle"):
        api._doctor_post("/doctor/repair", {"action": "ensure_private_project_state"})
    assert not (tmp_path / ".local-coder").exists()
    api.active_job = None
    handled, result = api._doctor_post("/doctor/repair", {"action": "ensure_private_project_state"})
    assert handled and result["verified"]
    assert result["before"]["checks"]["private_state"]["status"] == "warning"
    assert result["after"]["checks"]["private_state"]["status"] == "ok"


@pytest.mark.parametrize("body", [{}, {"action": []}, {"action": "chmod-root"}, {"action": "ensure_private_project_state", "path": "/tmp"}])
def test_repair_rejects_unknown_inputs(api, body):
    with pytest.raises(ValueError):
        api._doctor_post("/doctor/repair", body)


def test_repair_rechecks_action_and_hides_raw_errors(api):
    with pytest.raises(ValueError, match="not available"):
        api._doctor_post("/doctor/repair", {"action": "reset_failed_runtime"})
    api.inference.status = lambda: {"state": "error", "running": False, "settings": {}}
    def broken():
        raise ValueError("private-token")
    api.inference.stop = broken
    with pytest.raises(ValueError, match="could not be verified") as error:
        api._doctor_post("/doctor/repair", {"action": "reset_failed_runtime"})
    assert "private-token" not in str(error.value)

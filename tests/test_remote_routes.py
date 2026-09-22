"""Tests for the local-server/GPU HTTP routes RemoteControlServer.handle()
exposes for the browser UI's command bar and GPU usage indicator."""
from local_coder import local_server
from local_coder.remote import RemoteControlServer
from local_coder.types import AgentEvent


def _server(tmp_path):
    return RemoteControlServer(str(tmp_path))


def test_events_get_a_monotonic_seq_that_survives_ring_buffer_eviction(tmp_path):
    """events is a maxlen=200 ring buffer shared across every run this
    process has served -- a raw list index would silently shift once old
    entries start falling off the front. The browser UI's "only show
    events from this run" filter relies on `seq` being stable instead."""
    server = _server(tmp_path)
    server.events = server.events.__class__(maxlen=3)  # force eviction quickly

    for i in range(5):
        server._event(AgentEvent(source="ORCHESTRATOR", event_type="phase", message=f"step {i}"))

    _status, payload = server.handle("GET", "/events")
    seqs = [ev["seq"] for ev in payload["events"]]

    assert seqs == [3, 4, 5]  # events 1-2 evicted, but seq keeps counting up
    assert [ev["message"] for ev in payload["events"]] == ["step 2", "step 3", "step 4"]


def test_gpu_route_returns_local_server_gpu_status(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "gpu_status", lambda: {"available": True, "gpus": [{"name": "fake"}]})

    status, payload = _server(tmp_path).handle("GET", "/gpu")

    assert status == 200
    assert payload == {"available": True, "gpus": [{"name": "fake"}]}


def test_gpu_route_reports_unavailable_on_a_machine_with_no_gpu(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "gpu_status", lambda: {"available": False})

    status, payload = _server(tmp_path).handle("GET", "/gpu")

    assert status == 200
    assert payload == {"available": False}


def test_local_server_status_route(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)

    status, payload = _server(tmp_path).handle("GET", "/local-server/status")

    assert status == 200
    assert "big" in payload and "draft" in payload


def test_local_server_models_route(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "AI2_DIR", str(tmp_path))

    status, payload = _server(tmp_path).handle("GET", "/local-server/models")

    assert status == 200
    assert payload == {"models": []}


def test_local_server_start_route_rejects_unknown_quant(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)

    status, payload = _server(tmp_path).handle("POST", "/local-server/start", {"quant": "bogus"})

    assert status == 400
    assert "bogus" in payload["error"]


def test_local_server_switch_route_requires_quant(tmp_path):
    status, payload = _server(tmp_path).handle("POST", "/local-server/switch", {})

    assert status == 400
    assert "quant" in payload["error"]


def test_local_server_stop_route_stops_both_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(local_server, "stop", lambda which: calls.append(which) or {"status": "not_running"})

    status, payload = _server(tmp_path).handle("POST", "/local-server/stop", {})

    assert status == 200
    assert set(calls) == {"big", "draft"}
    assert payload["big"] == {"status": "not_running"}


def test_local_server_stop_route_can_target_just_one(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(local_server, "stop", lambda which: calls.append(which) or {"status": "not_running"})

    status, payload = _server(tmp_path).handle("POST", "/local-server/stop", {"draft": False})

    assert status == 200
    assert calls == ["big"]
    assert "draft" not in payload

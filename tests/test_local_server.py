"""Tests for the pure/local-filesystem parts of local_server.py -- the
PID-file lifecycle and quant discovery. Actually spawning llama-server is
not exercised here (that needs the real binary and GPU); it was verified
manually against the real local stack."""
import os

from local_coder import local_server


def test_list_available_models_only_returns_files_actually_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "AI2_DIR", str(tmp_path))
    quants_dir = tmp_path / "models" / "quants"
    quants_dir.mkdir(parents=True)
    # Only create the file for one of the known quants.
    only_quant_name, (filename, _note) = next(iter(local_server.QUANTS.items()))
    (quants_dir / filename).write_bytes(b"x" * 2048)

    available = local_server.list_available_models()

    assert [info.name for info in available] == [only_quant_name]
    assert available[0].size_gb == round(2048 / 1e9, 1)


def test_list_available_models_empty_when_quants_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "AI2_DIR", str(tmp_path))

    assert local_server.list_available_models() == []


def test_status_reports_not_running_with_no_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)

    state = local_server.status()

    assert state["big"]["pid"] is None
    assert state["big"]["process_running"] is False
    assert state["draft"]["pid"] is None


def test_status_detects_a_live_process(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)
    local_server._pid_file("big").write_text(str(os.getpid()))

    state = local_server.status()

    assert state["big"]["pid"] == os.getpid()
    assert state["big"]["process_running"] is True


def test_status_detects_a_stale_pid_as_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)
    # A PID essentially guaranteed not to correspond to a live process.
    local_server._pid_file("draft").write_text("999999")

    state = local_server.status()

    assert state["draft"]["pid"] == 999999
    assert state["draft"]["process_running"] is False


def test_stop_with_no_pid_file_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)

    result = local_server.stop("big")

    assert result == {"status": "not_running"}


def test_stop_cleans_up_a_stale_pid_file_without_erroring(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)
    local_server._pid_file("draft").write_text("999999")

    result = local_server.stop("draft")

    assert result == {"status": "not_running"}
    assert not local_server._pid_file("draft").exists()


def test_start_big_model_rejects_unknown_quant(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "STATE_DIR", tmp_path)

    try:
        local_server.start_big_model(quant="not-a-real-quant")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "not-a-real-quant" in str(exc)


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_gpu_status_parses_real_nvidia_smi_csv_shape(monkeypatch):
    """Shape actually observed from `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`
    on this machine's RTX 5070."""
    csv_line = "NVIDIA GeForce RTX 5070, 7, 10803, 12227, 31, 10.45, 175.00\n"

    def fake_run(cmd, capture_output, text, timeout):
        return _FakeCompletedProcess(0, csv_line)

    monkeypatch.setattr(local_server.subprocess, "run", fake_run)

    result = local_server.gpu_status()

    assert result["available"] is True
    gpu = result["gpus"][0]
    assert gpu["name"] == "NVIDIA GeForce RTX 5070"
    assert gpu["utilization_pct"] == 7.0
    assert gpu["memory_used_mb"] == 10803.0
    assert gpu["memory_total_mb"] == 12227.0
    assert gpu["power_limit_w"] == 175.0


def test_gpu_status_reports_unavailable_when_nvidia_smi_is_missing(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(local_server.subprocess, "run", fake_run)

    assert local_server.gpu_status() == {"available": False}


def test_gpu_status_reports_unavailable_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return _FakeCompletedProcess(1, "")

    monkeypatch.setattr(local_server.subprocess, "run", fake_run)

    assert local_server.gpu_status() == {"available": False}

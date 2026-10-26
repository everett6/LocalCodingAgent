"""Exercise lifecycle using a tiny authenticated Python fake, never a real model."""
import io
import json
import os
from pathlib import Path
import signal
import sys
import time
import zipfile

import httpx
import pytest

from local_coder.inference import EmbeddedInferenceManager, validate_model


HELP = """--model --host --port --ctx-size --api-key --api-key-file
--threads --gpu-layers --parallel --alias
--flash-attn [on|off|auto]
--cache-ram --cache-reuse --model-draft --spec-type draft-simple
"""


@pytest.fixture
def fixture_runtime(tmp_path):
    if os.name == "nt":
        pytest.skip("Fake executable uses a POSIX shebang; Windows requires packaged executable integration testing.")
    model = tmp_path / "target.gguf"
    model.write_bytes(b"GGUF" + b"\0" * 1024)
    draft = tmp_path / "draft.gguf"
    draft.write_bytes(b"GGUF" + b"\0" * 1024)
    runtime = tmp_path / "llama-server"
    runtime.write_text(f'''#!{sys.executable}
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
if "--help" in sys.argv:
    print({HELP!r})
    sys.exit(0)
def arg(name): return sys.argv[sys.argv.index(name)+1]
key = Path(arg("--api-key-file")).read_text().strip()
Path(__file__).with_suffix(".args").write_text(json.dumps(sys.argv[1:]))
if Path(__file__).with_suffix(".slow").exists(): time.sleep(15)
if Path(__file__).with_suffix(".child").exists():
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(__file__).with_suffix(".child-pid").write_text(str(p.pid))
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health" and self.headers.get("Authorization") != "Bearer " + key:
            self.send_response(401); self.end_headers(); return
        self.send_response(200); self.end_headers()
        self.wfile.write(b'{{"data":[{{"id":"app-local"}}]}}')
    def log_message(self, *args): pass
HTTPServer((arg("--host"), int(arg("--port"))), Handler).serve_forever()
''')
    runtime.chmod(0o755)
    manager = EmbeddedInferenceManager(tmp_path / "state")
    manager.configure({"runtime_path": str(runtime), "model_path": str(model), "prefetch_mb": 1})
    yield manager, runtime, model, draft
    manager.stop()


def wait_state(manager, states=("running", "error"), timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status()
        if status["state"] in states:
            return status
        time.sleep(.02)
    pytest.fail(f"Timed out waiting for {states}: {manager.status()}")


def test_real_loopback_start_auth_and_stop(fixture_runtime):
    manager, runtime, model, draft = fixture_runtime
    begun = time.monotonic()
    assert manager.start({"draft_model_path": str(draft)})["state"] == "starting"
    assert time.monotonic() - begun < 1
    state = wait_state(manager)
    assert state["state"] == "running", state
    assert state["capabilities"]["speculative_decoding"]
    assert state["profiling"]["startup_seconds"] > 0
    assert state["profiling"]["speedup_measured"] is False
    endpoint = manager.endpoint_config()
    assert endpoint["model"] == "app-local"
    assert endpoint["base_url"].startswith("http://127.0.0.1:")
    with httpx.Client(trust_env=False) as client:
        assert client.get(endpoint["base_url"] + "/models").status_code == 401
        assert client.get(endpoint["base_url"] + "/models", headers={"Authorization": "Bearer " + endpoint["api_key"]}).status_code == 200
    assert endpoint["api_key"] not in json.dumps(state)
    assert endpoint["api_key"] not in (manager.state_dir / "inference.json").read_text()
    assert endpoint["api_key"] not in runtime.with_suffix(".args").read_text()
    assert manager.stop()["state"] == "stopped"
    assert manager.endpoint_config() is None
    assert not list(manager.state_dir.glob("inference-key-*"))
    with pytest.raises(httpx.HTTPError):
        httpx.get(endpoint["base_url"] + "/models", timeout=.2, trust_env=False)


def test_cancel_startup(fixture_runtime):
    manager, runtime, *_ = fixture_runtime
    runtime.with_suffix(".slow").touch()
    manager.start()
    deadline = time.monotonic() + 2
    while manager._proc is None and time.monotonic() < deadline:
        time.sleep(.02)
    assert manager._proc is not None
    proc = manager._proc
    assert manager.stop()["state"] == "stopped"
    assert proc.poll() is not None
    assert manager.endpoint_config() is None


def test_prefetch_is_bounded_and_measured(fixture_runtime):
    manager, runtime, model, draft = fixture_runtime
    with model.open("ab") as stream:
        stream.truncate(3 * 1048576)
    manager.configure({"draft_model_path": str(draft)})
    deadline = time.monotonic() + 2
    while manager.status()["prefetch"]["state"] in ("idle", "reading") and time.monotonic() < deadline:
        time.sleep(.01)
    state = manager.status()["prefetch"]
    assert state["state"] == "complete"
    assert state["files"] == 2
    assert 0 < state["bytes_read"] <= 1048576
    assert "elapsed_seconds" in state


def test_capability_gating(fixture_runtime):
    manager, runtime, model, draft = fixture_runtime
    settings = dict(manager._settings, draft_model_path=str(draft))
    command, flags = manager._command(settings, "--model --host --port --ctx-size --api-key", 42, Path("/tmp/key"))
    assert "--model-draft" not in command
    assert "--flash-attn" not in command
    assert not manager.status()["capabilities"]["speculative_decoding"]
    assert "lacks a draft" in " ".join(manager.status()["capabilities"]["notes"])
    with pytest.raises(ValueError, match="authentication"):
        manager._command(settings, "--model --host --port --ctx-size", 42, Path("/tmp/key"))


def test_invalid_config_and_model_files(tmp_path):
    manager = EmbeddedInferenceManager(tmp_path / "state")
    with pytest.raises(ValueError, match="absolute"):
        validate_model("relative.gguf")
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"x" * 24)
    with pytest.raises(ValueError, match="header"):
        validate_model(str(bad))
    with pytest.raises(ValueError, match="prefetch_mb"):
        manager.configure({"prefetch_mb": 1025})
    with pytest.raises(ValueError, match="Unknown"):
        manager.configure({"api_key": "do-not-save"})
    with pytest.raises(ValueError, match="not installed"):
        manager.start()


def test_process_tree_shutdown(fixture_runtime):
    manager, runtime, *_ = fixture_runtime
    runtime.with_suffix(".child").touch()
    manager.start()
    assert wait_state(manager)["state"] == "running"
    child = int(runtime.with_suffix(".child-pid").read_text())
    manager.stop()
    # A killed orphan may remain a zombie until the host's init reaps it.
    proc_state = Path(f"/proc/{child}/stat")
    if proc_state.exists():
        assert proc_state.read_text().split()[2] == "Z"
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)


def test_archive_rejects_traversal_and_symlinks(tmp_path):
    manager = EmbeddedInferenceManager(tmp_path / "state")
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escaped", "bad")
    with pytest.raises(ValueError, match="Unsafe"):
        manager._extract_runtime(archive, tmp_path / "extract")
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        bundle.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="symbolic"):
        manager._extract_runtime(archive, tmp_path / "extract")
    assert not (tmp_path / "escaped").exists()


def test_official_asset_requires_digest(tmp_path, monkeypatch):
    manager = EmbeddedInferenceManager(tmp_path / "state")
    asset = {"name": "llama-bin-ubuntu-x64.tar.gz", "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b1/runtime.tar.gz"}
    monkeypatch.setattr("local_coder.inference.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("local_coder.inference.sys.platform", "linux")
    with pytest.raises(ValueError, match="checksum"):
        manager._select_asset([{"assets": [asset]}])
    asset["digest"] = "sha256:" + "0" * 64
    assert manager._select_asset([{"assets": [asset]}]) == asset
    asset["browser_download_url"] = "https://evil.test/runtime.tar.gz"
    with pytest.raises(ValueError, match="checksum"):
        manager._select_asset([{"assets": [asset]}])


def test_startup_timeout(fixture_runtime):
    manager, runtime, *_ = fixture_runtime
    runtime.with_suffix(".slow").touch()
    manager.start({"startup_timeout": 1})
    state = wait_state(manager, timeout=4)
    assert state["state"] == "error"
    assert "timed out" in state["error"]
    assert manager.endpoint_config() is None


def test_persistence_does_not_attach(fixture_runtime):
    manager, runtime, *_ = fixture_runtime
    manager.start()
    assert wait_state(manager)["state"] == "running"
    other = EmbeddedInferenceManager(manager.state_dir)
    assert other.status()["state"] == "stopped"
    assert other.endpoint_config() is None
    other.stop()
    assert manager.status()["running"]


def test_installer_download_verify_and_probe_without_network(tmp_path, monkeypatch):
    import hashlib
    manager = EmbeddedInferenceManager(tmp_path / "state")
    if os.name == "nt":
        pytest.skip("Fake executable uses a POSIX shebang.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bin/llama-server", f'#!{sys.executable}\nprint({HELP!r})\n')
    payload = buffer.getvalue()
    asset = {"name": "fixture.zip", "size": len(payload),
             "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
             "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b1/fixture.zip"}
    real_client = httpx.Client
    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=[{"assets": [asset]}])
        return httpx.Response(200, content=payload)
    monkeypatch.setattr("local_coder.inference.httpx.Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    assert manager.install_runtime("fixture.zip")["state"] in ("installing", "stopped")
    state = wait_state(manager, ("stopped", "error"))
    assert state["state"] == "stopped", state
    assert state["installation"]["checksum_verified"] is True
    assert Path(state["settings"]["runtime_path"]).is_file()
    assert state["installation"]["bytes_downloaded"] == len(payload)
    manager.stop()
    asset["digest"] = "sha256:" + "0" * 64
    manager.install_runtime("fixture.zip")
    state = wait_state(manager, ("stopped", "error"))
    assert state["state"] == "error"
    assert "checksum" in state["error"]
    manager.stop()


def test_runtime_ignoring_auth_is_rejected(fixture_runtime):
    manager, runtime, *_ = fixture_runtime
    runtime.write_text(runtime.read_text().replace('if self.path != "/health" and', 'if False and'))
    manager.start()
    state = wait_state(manager)
    assert state["state"] == "error"
    assert "authentication" in state["error"]
    assert manager.endpoint_config() is None


def test_runtime_death_clears_private_endpoint(fixture_runtime):
    manager, *_ = fixture_runtime
    manager.start()
    assert wait_state(manager)["state"] == "running"
    manager._proc.kill()
    manager._proc.wait()
    state = manager.status()
    assert state["state"] == "error"
    assert manager.endpoint_config() is None


def test_archive_materializes_internal_library_alias(tmp_path):
    manager = EmbeddedInferenceManager(tmp_path / "state")
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("bin/libggml.so.1", b"library")
        info = zipfile.ZipInfo("bin/libggml.so")
        info.external_attr = 0o120777 << 16
        bundle.writestr(info, "libggml.so.1")
    destination = tmp_path / "extracted"
    manager._extract_runtime(archive, destination)
    assert (destination / "bin/libggml.so").read_bytes() == b"library"
    assert not (destination / "bin/libggml.so").is_symlink()


def test_restart_rotates_credentials(fixture_runtime):
    manager, *_ = fixture_runtime
    manager.start()
    assert wait_state(manager)["state"] == "running"
    first = manager.endpoint_config()["api_key"]
    manager.stop()
    manager.start()
    assert wait_state(manager)["state"] == "running"
    assert manager.endpoint_config()["api_key"] != first

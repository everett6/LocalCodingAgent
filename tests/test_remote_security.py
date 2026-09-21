import pytest

from local_coder.models.manager import ModelManager
from local_coder.remote import RemoteControlServer
from local_coder.types import ModelConfig, ProjectConfig


def test_non_loopback_remote_server_requires_token(tmp_path):
    server = RemoteControlServer(str(tmp_path))

    with pytest.raises(ValueError, match="token"):
        server.serve("0.0.0.0", 0)


def test_remote_token_authorizes_requests(tmp_path):
    server = RemoteControlServer(str(tmp_path), token="secret")

    assert server.token == "secret"
    status, payload = server.handle("GET", "/status")

    assert status == 200
    assert payload["project_root"] == str(tmp_path)


def test_unknown_explicit_model_fails_fast(tmp_path):
    config = ProjectConfig(models={"coder": ModelConfig(name="coder", model_id="coder")})
    manager = ModelManager(config)

    async def run():
        return await manager.get_model("coder", "missing")

    import asyncio

    with pytest.raises(ValueError, match="Model not configured: missing"):
        asyncio.run(run())
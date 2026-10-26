import hashlib
import json
import os
from pathlib import Path

from local_coder.remote import RemoteControlServer


def test_authenticated_engine_publishes_private_connection_and_removes_it(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    workspace = tmp_path / 'project'; workspace.mkdir()
    control = RemoteControlServer(str(workspace), token='test-private-token')
    server = control.make_http_server('127.0.0.1', 0)
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    descriptor = Path.home() / '.local-coder/connections' / (key + '.json')
    try:
        assert control.handle('GET', '/identity') == (200, {'project_root': str(workspace), 'pid': os.getpid()})
        value = json.loads(descriptor.read_text())
        assert value == {'workspace': str(workspace.resolve()), 'origin': f'http://127.0.0.1:{server.server_port}', 'token': 'test-private-token', 'pid': os.getpid()}
        if os.name != 'nt':
            assert descriptor.stat().st_mode & 0o777 == 0o600
            assert descriptor.parent.stat().st_mode & 0o777 == 0o700
    finally:
        server.server_close()
    assert not descriptor.exists()


def test_anonymous_preview_does_not_publish_attach_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    server = RemoteControlServer(str(tmp_path)).make_http_server('127.0.0.1', 0)
    try:
        assert not list((Path.home()/'.local-coder/connections').glob('*.json'))
    finally:
        server.server_close()

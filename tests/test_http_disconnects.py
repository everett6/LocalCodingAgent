"""An expected client disconnect must not trigger another HTTP response."""
import io

import pytest

from local_coder.remote import RemoteControlServer


@pytest.mark.parametrize('method,path', [('GET', '/status'), ('GET', '/'), ('GET', '/icon.svg'), ('POST', '/settings')])
@pytest.mark.parametrize('disconnect', [BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError])
def test_closed_response_stream_is_closed_once_without_a_second_error_response(tmp_path, monkeypatch, method, path, disconnect):
    control = RemoteControlServer(str(tmp_path), token='synthetic-access')
    server = control.make_http_server('127.0.0.1', 0)
    monkeypatch.setattr(control, 'handle', lambda *args: (200, {'fixture': True}))
    handler = object.__new__(server.RequestHandlerClass)
    handler.server = server
    handler.path = path
    handler.headers = {'Host': f'127.0.0.1:{server.server_port}', 'Authorization': 'Bearer synthetic-access',
                       'Content-Type': 'application/json', 'Content-Length': '2'}
    handler.rfile = io.BytesIO(b'{}')
    handler.close_connection = False
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None

    class ClosedClient:
        def write(self, data):
            raise disconnect('synthetic closed client')

    handler.wfile = ClosedClient()
    try:
        getattr(handler, 'do_' + method)()
        assert statuses == [200]
        assert handler.close_connection is True
    finally:
        server.server_close()

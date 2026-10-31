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


@pytest.mark.parametrize('timed_out', [False, True])
def test_incomplete_or_timed_out_json_body_never_reaches_the_control_action(tmp_path, monkeypatch, timed_out):
    control = RemoteControlServer(str(tmp_path), token='synthetic-access')
    server = control.make_http_server('127.0.0.1', 0)
    actions = []
    monkeypatch.setattr(control, 'handle', lambda *args: actions.append(args) or (200, {}))
    handler = object.__new__(server.RequestHandlerClass)
    handler.server = server
    handler.path = '/settings'
    handler.headers = {'Host': f'127.0.0.1:{server.server_port}', 'Authorization': 'Bearer synthetic-access',
                       'Content-Type': 'application/json', 'Content-Length': '3'}
    handler.close_connection = False
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()

    class TimedOutBody:
        def read(self, length):
            raise TimeoutError('synthetic idle client')

    handler.rfile = TimedOutBody() if timed_out else io.BytesIO(b'{}')
    try:
        handler.do_POST()
        assert statuses == [408 if timed_out else 400]
        assert actions == []
        assert handler.close_connection is True
    finally:
        server.server_close()


@pytest.mark.parametrize('body', [b'{"value":NaN}', b'{"value":Infinity}', b'{"value":1,"value":2}',
                                b'{"value":' + b'[' * 1100 + b'0' + b']' * 1100 + b'}', b'{"value":1e999}'],
                         ids=['nan', 'infinity', 'duplicate-key', 'excessive-depth', 'numeric-overflow'])
def test_invalid_or_ambiguous_json_never_reaches_a_control_action(tmp_path, monkeypatch, body):
    control = RemoteControlServer(str(tmp_path), token='synthetic-access')
    server = control.make_http_server('127.0.0.1', 0)
    actions = []
    monkeypatch.setattr(control, 'handle', lambda *args: actions.append(args) or (200, {}))
    handler = object.__new__(server.RequestHandlerClass)
    handler.server = server
    handler.path = '/settings'
    handler.headers = {'Host': f'127.0.0.1:{server.server_port}', 'Authorization': 'Bearer synthetic-access',
                       'Content-Type': 'application/json', 'Content-Length': str(len(body))}
    handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    try:
        handler.do_POST()
        assert statuses == [400]
        assert actions == []
    finally:
        server.server_close()


def test_request_depth_preflight_ignores_brackets_inside_escaped_strings():
    import json
    from local_coder.remote import _validate_request_depth
    source = '"\\"' + '[{]} ' * 200
    _validate_request_depth(json.dumps({'request': source}).encode())

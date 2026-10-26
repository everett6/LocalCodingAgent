import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from local_coder.remote import RemoteControlServer


@pytest.fixture
def http_server(tmp_path):
    control = RemoteControlServer(str(tmp_path), token='test-access')
    server = control.make_http_server('127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_built_shell_assets_and_pwa_are_served(http_server):
    html = urllib.request.urlopen(http_server).read().decode()
    for path in re.findall(r'(?:src|href)="(/[^\"]+)"', html) + ['/sw.js', '/manifest.webmanifest']:
        response = urllib.request.urlopen(http_server + path)
        assert response.status == 200
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(http_server + '/settings')
    assert error.value.code == 401


def test_http_rejects_cross_origin_and_nonobject_bodies(http_server):
    for payload, origin, expected in [(b'[]', None, 400), (b'{}', 'https://evil.test', 403)]:
        request = urllib.request.Request(http_server + '/settings', data=payload, headers={
            'Authorization': 'Bearer test-access', 'Content-Type': 'application/json',
            **({'Origin': origin} if origin else {})})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == expected


def test_loopback_api_rejects_untrusted_host_header(http_server):
    request = urllib.request.Request(http_server + '/status', headers={'Authorization': 'Bearer test-access', 'Host': 'evil.test'})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 401

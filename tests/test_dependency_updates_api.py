from contextlib import nullcontext

import httpx
import pytest

from local_coder import dependency_updates_api as module


class API(module.DependencyUpdatesAPI):
    def __init__(self, root):
        self.project_root = root

    def _harness_idle(self):
        return nullcontext()


def test_status_is_offline_and_refresh_uses_only_fixed_source(tmp_path, monkeypatch):
    api = API(tmp_path)
    def forbidden(*args):
        pytest.fail('GET must not fetch')
    monkeypatch.setattr(module, '_fetch_osv', forbidden)
    handled, status = api._dependency_updates_get('/research/advisories/updates', {})
    assert handled and status['total'] == 0 and status['coverage'] == 'incomplete'
    for body in [{'source': 'https://evil.invalid', 'advisory_id': 'CVE-2026-12345'},
                 {'source': 'osv-record', 'advisory_id': '../secret'},
                 {'source': 'osv-record', 'advisory_id': 'CVE-2026-12345', 'url': 'https://evil.invalid'}]:
        with pytest.raises(ValueError):
            api._dependency_updates_post('/research/advisories/update', body)
    monkeypatch.setattr(module, '_fetch_osv', lambda identifier: ({'id': identifier, 'affected': [
        {'package': {'ecosystem': 'npm', 'name': 'alpha'}, 'versions': ['1.0.0']}]}, 'a' * 64))
    handled, result = api._dependency_updates_post('/research/advisories/update', {'source': 'osv-record', 'advisory_id': 'CVE-2026-12345'})
    assert handled and result['imported'] == 1 and result['coverage'] == 'incomplete'
    assert api._dependency_updates_get('/elsewhere', {}) == (False, None)
    assert api._dependency_updates_post('/elsewhere', {}) == (False, None)


@pytest.mark.parametrize('status,headers,content', [
    (302, {'location': 'https://evil.invalid'}, b''),
    (200, {'content-encoding': 'gzip'}, b'compressed'),
    (200, {'content-length': '1000001'}, b''),
    (200, {}, b'x' * 1000001),
    (200, {}, b'{"id":"unexpected"}'),
])
def test_download_boundaries_without_network(monkeypatch, status, headers, content):
    original = httpx.Client
    def client(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return original(**kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))))
    monkeypatch.setattr(module.httpx, 'Client', client)
    with pytest.raises(ValueError):
        module._fetch_osv('CVE-2026-12345')

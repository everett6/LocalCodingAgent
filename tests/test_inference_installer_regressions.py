"""Real archive/HTTP transport regressions; no downloads or executable probes."""
import hashlib
import io
import os
from pathlib import Path
import tarfile
import zipfile

import httpx
import pytest

from local_coder.inference import EmbeddedInferenceManager


def archive_bytes(kind):
    buffer = io.BytesIO()
    binary = b'\x7fELF' + bytes(100)
    executable = 'bin/llama-server.exe' if os.name == 'nt' else 'bin/llama-server'
    if kind == 'zip':
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(executable, binary)
    else:
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            item = tarfile.TarInfo(executable)
            item.size = len(binary)
            archive.addfile(item, io.BytesIO(binary))
    return buffer.getvalue()


@pytest.mark.parametrize('kind,encoding', [('zip', 'gzip'), ('tar', 'deflate')])
def test_transport_content_encoding_never_decompresses_verified_archive_bytes(tmp_path, monkeypatch, kind, encoding):
    payload = archive_bytes(kind)
    asset = {'name': 'fixture.zip' if kind == 'zip' else 'fixture.tar.gz', 'size': len(payload), 'digest': 'sha256:' + hashlib.sha256(payload).hexdigest(), 'browser_download_url': 'https://github.com/ggml-org/llama.cpp/releases/download/b1/fixture.zip'}
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    requests, probes = [], []
    real_client = httpx.Client
    def handler(request):
        requests.append(request)
        if request.url.host == 'api.github.com':
            return httpx.Response(200, json=[{'assets': [asset]}])
        # A CDN can label archive bytes with Content-Encoding. httpx.iter_bytes
        # attempts HTTP decompression, which corrupts the asset or raises zlib -3.
        return httpx.Response(200, headers={'Content-Encoding': encoding}, stream=httpx.ByteStream(payload))
    monkeypatch.setattr('local_coder.inference.httpx.Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(manager, '_help', lambda runtime, env: probes.append(runtime) or '--model --host')
    manager._install(asset['name'])
    status = manager.status()
    assert status['installation']['state'] == 'complete', status['error']
    assert status['installation']['checksum_verified'] is True
    assert status['installation']['bytes_downloaded'] == len(payload)
    assert requests[-1].headers['Accept-Encoding'] == 'identity'
    assert len(probes) == 1
    assert Path(status['settings']['runtime_path']).read_bytes().startswith(b'\x7fELF')


def test_checksum_failure_does_not_extract_probe_or_replace_selected_runtime(tmp_path, monkeypatch):
    payload = archive_bytes('zip')
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    manager._settings['runtime_path'] = '/previous/verified/llama-server'
    asset = {'name': 'fixture.zip', 'size': len(payload), 'digest': 'sha256:' + '0' * 64, 'browser_download_url': 'https://github.com/ggml-org/llama.cpp/releases/download/b1/fixture.zip'}
    real_client = httpx.Client
    def handler(request):
        return httpx.Response(200, json=[{'assets': [asset]}]) if request.url.host == 'api.github.com' else httpx.Response(200, stream=httpx.ByteStream(payload))
    monkeypatch.setattr('local_coder.inference.httpx.Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(manager, '_help', lambda *_args: pytest.fail('Unverified executable was probed'))
    monkeypatch.setattr(manager, '_extract_runtime', lambda *_args: pytest.fail('Unverified archive was extracted'))
    manager._install('fixture.zip')
    assert 'checksum' in manager.status()['error'].lower()
    assert manager.status()['settings']['runtime_path'] == '/previous/verified/llama-server'

"""Metadata-only fixtures: no model weights, detected binary execution or network."""
import json
import os
from pathlib import Path
import platform
import stat
import struct
import subprocess
import time

import pytest

from local_coder.inference_discovery import DiscoveryManager, inspect_gguf, inspect_runtime, recommend_context, read_locations_manifest


def gguf(path, context=131072, extra=b'', tensors=1):
    def string(value):
        value = value.encode()
        return struct.pack('<Q', len(value)) + value
    fields = [('general.name', 8, string('Discovery Fixture')), ('general.architecture', 8, string('llama')), ('llama.context_length', 4, struct.pack('<I', context))]
    data = struct.pack('<4sIQQ', b'GGUF', 3, tensors, len(fields) + bool(extra))
    data += b''.join(string(key) + struct.pack('<I', kind) + value for key, kind, value in fields)
    path.write_bytes(data + extra + bytes(128))
    return path


def runtime(path):
    header = bytearray(64)
    arm = platform.machine().lower() in ('arm64', 'aarch64')
    if os.name == 'nt':
        header[:2] = b'MZ'
        header[60:64] = struct.pack('<I', 64)
        header.extend(b'PE\0\0' + struct.pack('<H', 0xaa64 if arm else 0x8664))
    elif platform.system() == 'Darwin':
        header[:4] = b'\xcf\xfa\xed\xfe'
    else:
        header[:7] = b'\x7fELF\x02\x01\x01'
        header[18:20] = struct.pack('<H', 183 if arm else 62)
    path.write_bytes(header)
    path.chmod(0o755)
    return path


def wait(manager):
    deadline = time.monotonic() + 5
    while manager.status()['state'] == 'scanning' and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager.status()['state'] != 'scanning'
    return manager.status()


def test_gguf_header_reads_native_context_and_recommends_conservative_ram(tmp_path):
    model = gguf(tmp_path / 'model.gguf')
    metadata = inspect_gguf(model)
    assert metadata['valid'] and metadata['validity'] == 'metadata_valid'
    assert metadata['context_length'] == 131072
    assert metadata['architecture'] == 'llama'
    assert metadata['model_name'] == 'Discovery Fixture'
    assert metadata['metadata_bytes_read'] < 4096
    assert recommend_context(metadata, available_bytes=64 * 1024 ** 3) == 8192
    assert recommend_context(dict(metadata, size_bytes=16 * 1024 ** 3), available_bytes=2 * 1024 ** 3) == 512
    assert recommend_context(dict(metadata, context_length=None), available_bytes=64 * 1024 ** 3) == 4096


def test_metadata_bound_retains_early_fields_without_reading_large_weights(tmp_path):
    key = b'tokenizer.ggml.tokens'
    extra = struct.pack('<Q', len(key)) + key + struct.pack('<IIQ', 9, 8, 1) + struct.pack('<Q', 100 * 1024 ** 2)
    model = gguf(tmp_path / 'large.gguf', extra=extra)
    with model.open('ab') as output:
        # NTFS does not guarantee that extending an ordinary file is sparse.
        output.truncate(32 * 1024 ** (2 if os.name == 'nt' else 3))
    result = inspect_gguf(model)
    assert result['valid'] and result['metadata_partial']
    assert result['model_name'] == 'Discovery Fixture'
    assert result['context_length'] == 131072
    assert result['metadata_bytes_read'] <= 1048576


def test_vocab_only_and_wrong_magic_are_not_selectable_models(tmp_path):
    vocabulary = inspect_gguf(gguf(tmp_path / 'vocab.gguf', tensors=0))
    assert not vocabulary['valid'] and vocabulary['validity'] == 'vocabulary_only'
    invalid = tmp_path / 'invalid.gguf'
    invalid.write_bytes(b'not a GGUF model' + bytes(64))
    assert not inspect_gguf(invalid)['valid']


def test_scan_includes_huggingface_cache_and_never_executes_candidates(tmp_path, monkeypatch):
    cache = tmp_path / '.cache/huggingface/hub/models--fixture/snapshots/revision'
    cache.mkdir(parents=True)
    model = gguf(cache / 'cached.gguf')
    binary = runtime(tmp_path / 'llama-server')
    (tmp_path / 'libggml.so').write_bytes(b'metadata-only-library')
    def forbidden(*_args, **_kwargs):
        pytest.fail('Discovery must never execute a found runtime or subprocess')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env')
    manager.start([str(tmp_path)])
    status = wait(manager)
    assert status['state'] == 'complete'
    assert len(status['models']) == len(status['runtimes']) == 1
    assert status['models'][0]['path'] == str(model)
    assert status['runtimes'][0]['path'] == str(binary)
    assert status['runtimes'][0]['trusted'] is False
    assert status['runtimes'][0]['health'] == 'not_started'
    assert status['runtimes'][0]['libraries'][0]['name'] == 'libggml.so'
    manifest = tmp_path / 'private/locations.env'
    assert not manifest.stat().st_mode & 0o222
    if os.name != 'nt':
        assert stat.S_IMODE(manifest.stat().st_mode) == 0o400
        assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o700
    values = [json.loads(row.split('=', 1)[1]) for row in manifest.read_text().splitlines() if '=' in row]
    assert set(values) == {str(binary), str(model)}
    selection = manager.select(runtime_path=str(binary), model_path=str(model))
    assert selection['runtime_path'] == str(binary)
    assert 512 <= selection['context_size'] <= 8192
    assert 'LOCAL_CODER_SELECTED_MODEL_PATH=' in manifest.read_text()
    parsed = read_locations_manifest(manifest)
    assert parsed['LOCAL_CODER_SELECTED_MODEL_PATH'] == str(model)


def test_runtime_script_is_detected_but_not_treated_as_valid_binary(tmp_path):
    binary = tmp_path / 'llama-server'
    binary.write_text('#!/bin/sh\necho do not execute\n')
    binary.chmod(0o755)
    result = inspect_runtime(binary)
    assert not result['valid'] and result['format'] is None
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env')
    with pytest.raises(ValueError, match='binary header'):
        manager.select(runtime_path=str(binary))


def test_progress_limits_are_explicit_and_results_not_silently_capped(tmp_path):
    for index in range(20):
        gguf(tmp_path / f'model-{index}.gguf')
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env', max_entries=5)
    manager.start([str(tmp_path)])
    limited = wait(manager)
    assert limited['truncated'] is True
    assert limited['limits']['max_entries'] == 5
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env', max_entries=100)
    manager.start([str(tmp_path)])
    complete = wait(manager)
    assert not complete['truncated'] and len(complete['models']) == 20


def test_directory_symlinks_are_not_traversed_and_manifest_symlink_is_rejected(tmp_path):
    nested = tmp_path / 'tree'
    nested.mkdir()
    (nested / 'loop').symlink_to(nested, target_is_directory=True)
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env')
    manager.start([str(nested)])
    assert wait(manager)['scanned_directories'] == 1
    (tmp_path / 'redirect').symlink_to(tmp_path / 'private', target_is_directory=True)
    manager = DiscoveryManager(manifest_path=tmp_path / 'redirect/locations.env')
    with pytest.raises(ValueError, match='symlink'):
        manager.select(model_path=str(gguf(tmp_path / 'model.gguf')))


def test_cancellation_and_incremental_cache(tmp_path, monkeypatch):
    import local_coder.inference_discovery as discovery
    for index in range(10):
        gguf(tmp_path / f'model-{index}.gguf')
    real = discovery.inspect_gguf
    calls = []
    def delayed(path):
        calls.append(str(path))
        time.sleep(.01)
        return real(path)
    monkeypatch.setattr(discovery, 'inspect_gguf', delayed)
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env')
    manager.start([str(tmp_path)])
    time.sleep(.015)
    manager.cancel()
    assert wait(manager)['state'] == 'cancelled'
    manager.start([str(tmp_path)])
    assert len(wait(manager)['models']) == 10
    count = len(calls)
    manager.start([str(tmp_path)])
    assert len(wait(manager)['models']) == 10
    assert len(calls) == count


def test_manifest_values_are_data_and_never_shell_commands(tmp_path):
    manifest = tmp_path / 'locations.env'
    marker = tmp_path / 'never-created'
    value = str(tmp_path / 'models' / '$(touch never-created).gguf')
    manifest.write_text('LOCAL_CODER_MODEL_00000=' + json.dumps(value) + '\n')
    manifest.chmod(0o400)
    assert read_locations_manifest(manifest)['LOCAL_CODER_MODEL_00000'] == value
    assert not marker.exists()
    manifest.chmod(0o600)
    manifest.write_text('API_KEY="must-not-be-accepted"\n')
    manifest.chmod(0o400)
    with pytest.raises(ValueError, match='unsupported keys'):
        read_locations_manifest(manifest)


def test_windows_manifest_replaces_readonly_file_and_recovers_selection(tmp_path, monkeypatch):
    import local_coder.inference_discovery as module

    monkeypatch.setattr(module, '_WINDOWS', True)
    replace = os.replace
    replacements = []

    def windows_replace(source, destination):
        if Path(destination).exists():
            assert Path(destination).stat().st_mode & 0o200
        replacements.append(str(destination))
        return replace(source, destination)

    monkeypatch.setattr(module.os, 'replace', windows_replace)
    manifest = tmp_path / 'private/locations.env'
    manager = DiscoveryManager(manifest_path=manifest)
    model = gguf(tmp_path / 'model.gguf')
    manager.select(model_path=str(model))
    manager.select(model_path=str(model))
    assert len(replacements) == 2
    assert not manifest.stat().st_mode & 0o222
    assert read_locations_manifest(manifest)['LOCAL_CODER_SELECTED_MODEL_PATH'] == str(model)
    restored = DiscoveryManager(manifest_path=manifest)
    assert restored._selection['model_path'] == str(model)

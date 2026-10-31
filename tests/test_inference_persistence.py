"""Runtime setup failure keeps the previous selection and unrelated files."""
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from local_coder.inference import EmbeddedInferenceManager
from tests.test_inference_installer_regressions import archive_bytes


def test_configure_does_not_follow_a_predictable_temporary_symlink(tmp_path):
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    notes = tmp_path / 'notes.json'
    notes.write_text('unrelated fixture notes')
    try:
        (manager.state_dir / 'inference.json.tmp').symlink_to(notes)
    except OSError:
        pytest.skip('symlinks unavailable')
    manager.configure({'prefetch': False})
    assert notes.read_text() == 'unrelated fixture notes'


def setup_install(tmp_path, monkeypatch):
    payload = archive_bytes('zip')
    asset = {'name': 'fixture.zip', 'size': len(payload),
             'digest': 'sha256:' + hashlib.sha256(payload).hexdigest(),
             'browser_download_url': 'https://github.com/ggml-org/llama.cpp/releases/download/b1/fixture.zip'}
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    monkeypatch.setattr(manager, '_environment', lambda *args: {})
    manager._settings.update(runtime_path='/previous/verified/llama-server', prefetch=False)
    original = json.dumps(manager._settings)
    (manager.state_dir / 'inference.json').write_text(original)
    client = httpx.Client

    def handler(request):
        return (httpx.Response(200, json=[{'assets': [asset]}]) if request.url.host == 'api.github.com'
                else httpx.Response(200, stream=httpx.ByteStream(payload)))

    monkeypatch.setattr('local_coder.inference.httpx.Client', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    return manager, original


def test_install_save_failure_preserves_memory_disk_and_previous_runtime(tmp_path, monkeypatch):
    manager, original = setup_install(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, '_help', lambda *args: '--model --host')
    replace = os.replace

    def fail_settings(source, target):
        if Path(target) == manager.state_dir / 'inference.json':
            raise OSError('synthetic settings replacement failure')
        return replace(source, target)

    monkeypatch.setattr(os, 'replace', fail_settings)
    manager._install('fixture.zip')
    assert manager.status()['installation']['state'] == 'error'
    assert manager.status()['settings']['runtime_path'] == '/previous/verified/llama-server'
    assert (manager.state_dir / 'inference.json').read_text() == original
    assert not list(manager.state_dir.glob('runtime-*'))


def test_cancelled_extraction_does_not_probe_or_select_the_runtime(tmp_path, monkeypatch):
    manager, original = setup_install(tmp_path, monkeypatch)
    extract = manager._extract_runtime

    def cancel_after_extract(*args):
        extract(*args)
        manager._cancel.set()

    monkeypatch.setattr(manager, '_extract_runtime', cancel_after_extract)
    monkeypatch.setattr(manager, '_help', lambda *args: pytest.fail('Cancelled installation probed executable'))
    manager._install('fixture.zip')
    assert manager.status()['installation']['state'] == 'cancelled'
    assert manager.status()['settings']['runtime_path'] == '/previous/verified/llama-server'
    assert (manager.state_dir / 'inference.json').read_text() == original


def test_already_cancelled_capability_probe_never_spawns(tmp_path, monkeypatch):
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    manager._cancel.set()
    monkeypatch.setattr('local_coder.inference.subprocess.Popen', lambda *args, **kwargs: pytest.fail('Cancelled probe spawned'))
    with pytest.raises(RuntimeError, match='cancel'):
        manager._help('synthetic-runtime', {})


def test_install_never_deletes_a_colliding_destination_it_did_not_create(tmp_path, monkeypatch):
    manager, original = setup_install(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, '_help', lambda *args: '--model --host')
    monkeypatch.setattr('local_coder.inference.secrets.token_hex', lambda *args: 'collision')
    existing = manager.state_dir / 'runtime-collision'
    existing.mkdir()
    (existing / 'notes.txt').write_text('unrelated fixture notes')
    manager._install('fixture.zip')
    assert manager.status()['installation']['state'] == 'error'
    assert (existing / 'notes.txt').read_text() == 'unrelated fixture notes'
    assert (manager.state_dir / 'inference.json').read_text() == original


def test_settings_save_is_private_atomic_and_cleans_failed_staging(tmp_path, monkeypatch):
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    manager.configure({'prefetch': False})
    config = manager.state_dir / 'inference.json'
    original = config.read_bytes()
    if os.name != 'nt':
        assert config.stat().st_mode & 0o777 == 0o600
    replace = os.replace

    def fail_settings(source, target):
        if Path(target) == config:
            raise OSError('synthetic replacement failure')
        return replace(source, target)

    monkeypatch.setattr(os, 'replace', fail_settings)
    with pytest.raises(OSError):
        manager.configure({'threads': 1})
    assert config.read_bytes() == original
    assert not list(manager.state_dir.glob('.inference-*.tmp'))


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo'])
def test_settings_save_rejects_linked_or_special_targets_without_changing_notes(tmp_path, kind):
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    notes = tmp_path / 'notes.json'
    notes.write_text('unrelated fixture notes')
    target = manager.state_dir / 'inference.json'
    try:
        if kind == 'symlink':
            target.symlink_to(notes)
        elif kind == 'hardlink':
            os.link(notes, target)
        elif hasattr(os, 'mkfifo'):
            os.mkfifo(target)
        else:
            pytest.skip('FIFOs unavailable')
    except OSError:
        pytest.skip('test link unavailable')
    with pytest.raises(ValueError, match='ordinary'):
        manager.configure({'prefetch': False})
    assert notes.read_text() == 'unrelated fixture notes'


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'oversized', 'invalid'])
def test_settings_load_ignores_unsafe_or_invalid_files_with_a_safe_diagnostic(tmp_path, kind):
    state = tmp_path / 'state'
    state.mkdir()
    target = state / 'inference.json'
    notes = tmp_path / 'notes.json'
    notes.write_text('{"threads": 29, "model_path": "private-fixture-sentinel"}')
    try:
        if kind == 'symlink':
            target.symlink_to(notes)
        elif kind == 'hardlink':
            os.link(notes, target)
        else:
            target.write_text('x' * (65537 if kind == 'oversized' else 1))
    except OSError:
        pytest.skip('test links unavailable')
    manager = EmbeddedInferenceManager(state)
    assert manager.status()['settings']['threads'] != 29
    assert manager.status()['error']
    assert 'private-fixture-sentinel' not in manager.status()['error']


def test_constructor_rejects_linked_state_ancestor_before_creating_directory(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    linked = tmp_path / 'linked'
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError, match='linked'):
        EmbeddedInferenceManager(linked / 'new-state')
    assert not (actual / 'new-state').exists()


def test_installer_rejects_replaced_linked_storage_before_http(tmp_path, monkeypatch):
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    manager.state_dir.rmdir()
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    try:
        manager.state_dir.symlink_to(unrelated, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    monkeypatch.setattr('local_coder.inference.httpx.Client', lambda **kwargs: pytest.fail('Unsafe storage initiated HTTP'))
    manager._install('fixture.zip')
    assert manager.status()['installation']['state'] == 'error'
    assert 'linked' in manager.status()['error']
    assert not list(unrelated.iterdir())


def test_extraction_rejects_linked_parent_without_writing(tmp_path):
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    archive = tmp_path / 'fixture.zip'
    archive.write_bytes(archive_bytes('zip'))
    destination = tmp_path / 'extracted'
    destination.mkdir()
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    try:
        (destination / 'bin').symlink_to(unrelated, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError, match='linked'):
        manager._extract_runtime(archive, destination)
    assert not list(unrelated.iterdir())


def test_saved_settings_round_trip_without_diagnostic(tmp_path):
    manager = EmbeddedInferenceManager(tmp_path / 'state')
    manager.configure({'prefetch': False, 'threads': 3})
    loaded = EmbeddedInferenceManager(manager.state_dir)
    assert loaded.status()['settings']['threads'] == 3
    assert loaded.status()['error'] is None

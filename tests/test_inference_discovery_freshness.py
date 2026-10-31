"""Discovery metadata caches cannot bless replaced files or grow indefinitely."""
import os

import pytest

from local_coder.inference_discovery import DiscoveryManager
from tests.test_inference_discovery import gguf, wait


@pytest.mark.parametrize('replace', [False, True])
def test_same_size_restored_mtime_model_changes_invalidate_discovery_cache(tmp_path, replace):
    model = gguf(tmp_path / 'model.gguf', context=131072)
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env')
    manager.start([str(tmp_path)])
    assert wait(manager)['models'][0]['context_length'] == 131072
    before = model.stat()
    replacement = tmp_path / 'replacement.tmp' if replace else model
    gguf(replacement, context=262144)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    if replace:
        os.replace(replacement, model)
    manager.start([str(tmp_path)])
    assert wait(manager)['models'][0]['context_length'] == 262144


def test_cache_retention_is_bounded_without_capping_discovery_results(tmp_path, monkeypatch):
    monkeypatch.setattr('local_coder.inference_discovery.MAX_DISCOVERY_CACHE_ENTRIES', 2, raising=False)
    for index in range(8):
        gguf(tmp_path / f'model-{index}.gguf')
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env')
    manager.start([str(tmp_path)])
    assert len(wait(manager)['models']) == 8
    assert len(manager._cache) <= 2


def test_oversized_cache_records_are_not_retained_but_still_returned(tmp_path, monkeypatch):
    monkeypatch.setattr('local_coder.inference_discovery.MAX_DISCOVERY_CACHE_BYTES', 1)
    gguf(tmp_path / 'model.gguf')
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env')
    manager.start([str(tmp_path)])
    assert len(wait(manager)['models']) == 1
    assert not manager._cache and manager._cache_bytes == 0


def test_repeated_versions_keep_only_the_latest_cache_record_for_a_path(tmp_path):
    model = gguf(tmp_path / 'model.gguf')
    manager = DiscoveryManager(manifest_path=tmp_path / 'private/locations.env')
    for context in (1024, 2048, 4096):
        gguf(model, context=context)
        manager.start([str(tmp_path)])
        assert wait(manager)['models'][0]['context_length'] == context
    assert len(manager._cache) == 1


@pytest.mark.parametrize('entries,seconds', [(True, 1), (0, 1), (-1, 1), (5, float('nan')), (5, float('inf')), (5, 0)])
def test_discovery_rejects_invalid_resource_bounds(tmp_path, entries, seconds):
    with pytest.raises(ValueError, match='bound'):
        DiscoveryManager(manifest_path=tmp_path / 'private/locations.env', max_entries=entries, max_seconds=seconds)

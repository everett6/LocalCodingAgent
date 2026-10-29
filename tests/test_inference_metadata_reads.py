"""A local file replacement cannot block metadata inspection on a FIFO."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO regression')
@pytest.mark.parametrize('inspector', ['inspect_gguf', 'inspect_runtime'])
def test_metadata_inspection_rejects_special_file_replacement_without_blocking(tmp_path, inspector):
    candidate = tmp_path / ('model.gguf' if inspector == 'inspect_gguf' else 'llama-server')
    candidate.write_bytes(b'GGUF' + bytes(64))
    script = '''
import os, sys
from pathlib import Path
import local_coder.inference_discovery as discovery
original = discovery._regular
def replaced(value):
    path = original(value)
    path.unlink()
    os.mkfifo(path)
    return path
discovery._regular = replaced
try:
    getattr(discovery, sys.argv[2])(sys.argv[1])
except (ValueError, OSError):
    raise SystemExit(0)
raise SystemExit(2)
'''
    try:
        result = subprocess.run([sys.executable, '-c', script, str(candidate), inspector],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail('Metadata inspection blocked on a replaced special file')
    assert result.returncode == 0, 'Special-file replacement was not rejected safely'


def test_metadata_descriptor_rejects_file_change_during_inspection(tmp_path):
    from local_coder.inference_discovery import open_regular_candidate
    candidate = tmp_path / 'model.gguf'
    candidate.write_bytes(b'GGUF' + bytes(64))
    before = candidate.stat()
    with pytest.raises(ValueError, match='changed'):
        with open_regular_candidate(candidate) as (stream, _info):
            stream.read(4)
            candidate.write_bytes(b'GGUF' + bytes(128))
            os.utime(candidate, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO regression')
def test_settings_load_does_not_block_on_fifo(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    os.mkfifo(state / 'inference.json')
    script = '''
import sys
from local_coder.inference import EmbeddedInferenceManager
manager = EmbeddedInferenceManager(sys.argv[1])
assert manager.status()['error']
assert manager.status()['state'] == 'stopped'
'''
    try:
        result = subprocess.run([sys.executable, '-c', script, str(state)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail('Inference initialization blocked on settings FIFO')
    assert result.returncode == 0, 'Unsafe settings were not ignored safely'

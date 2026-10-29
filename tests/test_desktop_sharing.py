"""Exercise the real Python HTTP engine through both production JS clients."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest


def test_two_apps_share_one_engine_in_either_order_and_during_race(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for cross-language desktop integration')
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / 'home'; home.mkdir()
    result = subprocess.run(
        [node, str(root/'desktop/tests/fixtures/real-engine-sharing.cjs'), sys.executable, '--python'],
        env=dict(os.environ, HOME=str(home), PYTHONPATH=str(root/'src')),
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    for order in ['manager-first', 'ide-first', 'simultaneous']:
        assert order + ': one authenticated engine' in result.stdout
    assert 'owner-exit: explicit reconnect recovered one authenticated engine' in result.stdout
    assert not list((home/'.local-coder/connections').glob('*.json'))

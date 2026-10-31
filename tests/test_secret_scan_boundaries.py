"""Secret scans prune dependencies and fail explicitly on enumeration limits."""
import asyncio
import os
from pathlib import Path

import pytest

from local_coder.tools.security import SecurityScanTool, scan_secrets


def test_secret_scan_does_not_traverse_excluded_dependency_directories(tmp_path, monkeypatch):
    dependency = tmp_path / 'node_modules' / 'vendor'
    dependency.mkdir(parents=True)
    (dependency / 'ignored.py').write_text('value = 1')
    (tmp_path / 'source.py').write_text('value = 2')
    original = os.scandir
    opened = []

    def tracked(path):
        if not isinstance(path, int):
            opened.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, 'scandir', tracked)
    assert scan_secrets(tmp_path, [tmp_path]) == []
    assert not any(path == dependency.parent or path.is_relative_to(dependency.parent) for path in opened)


def test_secret_scan_skips_internal_symlink_aliases(tmp_path):
    (tmp_path / 'source.py').write_text('password = "synthetic-fixture-only"')
    try:
        (tmp_path / 'alias.py').symlink_to(tmp_path / 'source.py')
    except OSError:
        pytest.skip('symlinks unavailable')
    assert [finding.path for finding in scan_secrets(tmp_path, [tmp_path])] == ['source.py']


def test_secret_scan_has_an_explicit_global_enumeration_limit(tmp_path, monkeypatch):
    for index in range(4):
        (tmp_path / f'file-{index}.py').write_text('value = 1')
    monkeypatch.setattr('local_coder.tools.security.MAX_SECRET_SCAN_ENTRIES', 2, raising=False)
    result = asyncio.run(SecurityScanTool(str(tmp_path)).execute(scanners=['secrets']))
    assert result.success is False
    assert 'enumeration limit' in result.output
    assert 'narrow' in result.output.lower()


def test_secret_scan_reads_bounded_snapshots_instead_of_unbounded_path_reads(tmp_path, monkeypatch):
    (tmp_path / 'source.py').write_text('password = "synthetic-fixture-only"')

    def unbounded_read(path):
        pytest.fail('A source scanner must enforce its byte allowance during the read')

    monkeypatch.setattr(Path, 'read_bytes', unbounded_read)
    findings = scan_secrets(tmp_path, [tmp_path])
    assert [finding.path for finding in findings] == ['source.py']

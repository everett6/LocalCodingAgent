"""Offline advisory and manifest boundary regression coverage."""
import asyncio
import json
import os

import pytest

from local_coder.security_intelligence import DependencyInventory, LocalAdvisoryStore
from local_coder.tools.dependency_intelligence import DependencyIntelligenceTool


def test_import_is_atomic_and_replaces_only_same_advisory_package(tmp_path):
    store = LocalAdvisoryStore(tmp_path)
    base = {'id': 'CVE-2026-12345', 'ecosystem': 'npm', 'package': 'alpha', 'versions': ['1.0.0']}
    store.import_records({'version': 1, 'advisories': [base]})
    with pytest.raises(ValueError):
        store.import_records({'version': 1, 'advisories': [base, base | {'api_key': 'secret'}]})
    assert len(store.query()['matches']) == 1
    store.import_records({'version': 1, 'advisories': [base | {'versions': ['2.0.0']}]})
    assert not store.query(ecosystem='npm', package='alpha', version='1.0.0')['matches']
    cache = tmp_path / '.local-coder/security/advisories.json'
    if os.name != 'nt':
        assert cache.stat().st_mode & 0o777 == 0o600


def test_audit_uses_single_cache_snapshot_and_retains_conditional_status(tmp_path, monkeypatch):
    (tmp_path / 'requirements.txt').write_text("alpha==1.0.0; python_version < '3.14'\nbeta==2.0.0\n")
    store = LocalAdvisoryStore(tmp_path)
    store.import_records({'version': 1, 'advisories': [{'id': 'CVE-2026-12345', 'ecosystem': 'pypi', 'package': 'alpha', 'versions': ['1.0.0']}]})
    original = LocalAdvisoryStore._load
    calls = []
    def load(self):
        calls.append(True)
        return original(self)
    monkeypatch.setattr(LocalAdvisoryStore, '_load', load)
    result = asyncio.run(DependencyIntelligenceTool(str(tmp_path)).execute(action='audit'))
    assert result.success and len(calls) == 1
    assert result.data['matches'][0]['conditional'] is True
    assert result.data['matches'][0]['verification'] == 'unverified'
    assert result.data['coverage']['status'] == 'partial'


def test_unsupported_and_no_manifests_never_claim_complete_coverage(tmp_path):
    assert DependencyInventory(tmp_path).scan()['coverage']['status'] == 'partial'
    (tmp_path / 'poetry.lock').write_text('ignored')
    report = DependencyInventory(tmp_path).scan()
    assert 'unsupported lock format' in ' '.join(report['coverage']['skipped'])


def test_manifest_symlink_inside_workspace_is_not_followed(tmp_path):
    (tmp_path / 'actual.txt').write_text('private==1.0.0')
    (tmp_path / 'requirements.txt').symlink_to('actual.txt')
    report = DependencyInventory(tmp_path).scan()
    assert not report['components']
    assert report['coverage']['status'] == 'partial'


def test_corrupted_cache_unknown_fields_and_duplicate_json_are_rejected(tmp_path):
    path = tmp_path / '.local-coder/security/advisories.json'
    path.parent.mkdir(parents=True)
    path.write_text('{"version":1,"version":2}')
    with pytest.raises(ValueError):
        LocalAdvisoryStore(tmp_path).query()
    path.write_text(json.dumps({'version': 1, 'advisories': [], 'imported_at': [], 'source_label': 'fixture'}))
    with pytest.raises(ValueError):
        LocalAdvisoryStore(tmp_path).query()


@pytest.mark.parametrize('ranges', [False, True])
def test_audit_caps_cross_product_before_constructing_excess_records(tmp_path, monkeypatch, ranges):
    import local_coder.tools.dependency_intelligence as module
    monkeypatch.setattr(module, '_MAX_AUDIT_RECORDS', 7)
    (tmp_path / 'requirements.txt').write_text('alpha==1.0.0\n' * 20)
    advisories = [{'id': f'CVE-2026-{index:04d}', 'ecosystem': 'pypi', 'package': 'alpha',
                   'versions': ['1.0.0'], 'affected_ranges': ['<2'] if ranges else []}
                  for index in range(1000, 1020)]
    LocalAdvisoryStore(tmp_path).import_records({'version': 1, 'advisories': advisories})
    result = asyncio.run(DependencyIntelligenceTool(str(tmp_path)).execute(action='audit'))
    assert result.success
    assert len(result.data['matches']) + len(result.data['unresolved']) == 7
    assert result.data['coverage']['status'] == 'partial'
    assert any('audit record or comparison budget reached' in value for value in result.data['coverage']['skipped'])
    assert result.data['no_match_is_not_safety_evidence'] is True


def test_audit_caps_comparisons_when_versions_do_not_match(tmp_path, monkeypatch):
    import local_coder.tools.dependency_intelligence as module
    monkeypatch.setattr(module, '_MAX_AUDIT_COMPARISONS', 3)
    (tmp_path / 'requirements.txt').write_text('alpha==1.0.0\n' * 20)
    LocalAdvisoryStore(tmp_path).import_records({'version': 1, 'advisories': [
        {'id': 'CVE-2026-12345', 'ecosystem': 'pypi', 'package': 'alpha', 'versions': ['2.0.0']}]})
    result = asyncio.run(DependencyIntelligenceTool(str(tmp_path)).execute(action='audit'))
    assert result.success and not result.data['matches']
    assert result.data['coverage']['status'] == 'partial'
    assert any('comparison budget reached' in value for value in result.data['coverage']['skipped'])

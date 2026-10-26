import asyncio
import json
import socket
import subprocess

import pytest

from local_coder.security_intelligence import DependencyInventory, LocalAdvisoryStore
from local_coder.tools.dependency_intelligence import DependencyIntelligenceTool


def call(tmp_path, **kwargs):
    return asyncio.run(DependencyIntelligenceTool(str(tmp_path)).execute(**kwargs))


def test_package_lock_captures_exact_direct_and_transitive_versions_without_resolved_urls(tmp_path):
    secret = 'registry-private-token-value'
    lock = {'name': 'fixture', 'lockfileVersion': 3, 'packages': {
        '': {'dependencies': {'alpha': '^1.0.0'}},
        'node_modules/alpha': {'version': '1.2.3', 'dependencies': {'beta': '^2'}, 'resolved': f'https://user:{secret}@registry.invalid/alpha'},
        'node_modules/beta': {'version': '2.1.0'},
        'node_modules/alpha/node_modules/beta': {'version': '2.0.1'},
    }}
    (tmp_path / 'package-lock.json').write_text(json.dumps(lock))
    report = DependencyInventory(tmp_path).scan()
    components = report['components']
    assert len(components) == 3
    assert {(item['name'], item['version'], item['direct']) for item in components} == {('alpha', '1.2.3', True), ('beta', '2.1.0', False), ('beta', '2.0.1', False)}
    assert all(item['resolution'] == 'locked_exact' for item in components)
    alpha = next(item for item in components if item['name'] == 'alpha')
    nested = next(item for item in components if item['version'] == '2.0.1')
    assert any(edge['from'] == alpha['id'] and edge['to'] == nested['id'] for edge in report['relationships'])
    assert secret not in json.dumps(report)
    assert report['format'] == 'local-coder-dependency-inventory-v1'
    assert report['sbom_standard'] is None


def test_pyproject_requirements_pins_ranges_markers_and_urls_remain_honest(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('''[project]
name="fixture"
dependencies=["Alpha_Pkg==1.2.0", "ranged>=2,<3", "conditional==4.1; python_version < '3.14'"]
[project.optional-dependencies]
dev=["pytest==8.2.0"]
''')
    (tmp_path / 'requirements.txt').write_text('other==2.0\nunpinned\nurlpkg @ https://user:private-secret@invalid/pkg.whl\n-r ../outside.txt\n')
    report = DependencyInventory(tmp_path).scan()
    by_name = {item['name']: item for item in report['components']}
    assert by_name['alpha-pkg']['version'] == '1.2.0'
    assert by_name['pytest']['scope'] == 'optional:dev'
    assert by_name['ranged']['version'] is None
    assert by_name['conditional']['conditional'] is True
    assert by_name['urlpkg']['version'] is None
    assert report['unresolved'] and report['coverage']['status'] == 'partial'
    assert 'private-secret' not in json.dumps(report)
    assert all(item['provenance']['source_sha256'] for item in report['components'])


def test_cache_import_and_query_match_exact_versions_only_and_never_claim_clean(tmp_path):
    store = LocalAdvisoryStore(tmp_path)
    payload = {'version': 1, 'advisories': [
        {'id': 'CVE-2026-12345', 'ecosystem': 'PyPI', 'package': 'Alpha_Pkg', 'versions': ['1.2.0'], 'summary': 'Synthetic local advisory', 'severity': 'high'},
        {'id': 'GHSA-abcd-1234-efgh', 'ecosystem': 'pypi', 'package': 'alpha-pkg', 'affected_ranges': ['<2.0']},
    ]}
    imported = store.import_records(payload, source_label='operator fixture')
    assert imported['imported'] == 2
    result = store.query(ecosystem='pypi', package='alpha.pkg', version='1.2.0')
    assert len(result['matches']) == 1 and result['matches'][0]['match_type'] == 'exact_imported_version'
    assert result['unresolved']
    assert result['verification'] == 'unverified'
    assert not store.query(ecosystem='pypi', package='alpha-pkg', version='1.2')['matches']
    missing = store.query(ecosystem='npm', package='absent', version='1.0.0')
    assert missing['status'] == 'package_not_in_cache'
    assert missing['no_match_is_not_safety_evidence'] is True
    assert store.query(advisory_id='CVE-2026-12345')['matches'][0]['id'] == 'CVE-2026-12345'


def test_missing_cache_and_unresolved_components_are_not_false_negative_results(tmp_path):
    (tmp_path / 'requirements.txt').write_text('alpha>=1\nbeta==2.0\n')
    result = call(tmp_path, action='audit')
    assert result.success
    assert result.data['cache_status'] == 'cache_absent'
    assert result.data['unresolved']
    assert result.data['coverage']['status'] == 'partial'
    assert not (tmp_path / '.local-coder').exists(), 'Read-only actions cannot create advisory state'


def test_human_import_rejects_unknown_fields_ranges_as_versions_and_secret_reference_urls(tmp_path):
    store = LocalAdvisoryStore(tmp_path)
    base = {'id': 'CVE-2026-12345', 'ecosystem': 'npm', 'package': 'fixture', 'versions': ['1.0.0']}
    for changed in [base | {'versions': ['<2']}, base | {'reference': 'https://user:password@example.com'}, base | {'api_key': 'secret'}, base | {'id': '../outside'}, base | {'versions': ['1.*']}]:
        with pytest.raises(ValueError):
            store.import_records({'version': 1, 'advisories': [changed]})
    assert store.query(advisory_id='CVE-2026-12345')['cache_status'] == 'cache_absent'


def test_workspace_scope_symlink_manifest_and_advisory_store_are_checked(tmp_path):
    (tmp_path / 'package-lock.json').symlink_to(tmp_path.parent / 'outside.json')
    assert not call(tmp_path, target_path='../outside').success
    assert not call(tmp_path, target_path='package-lock.json').success
    (tmp_path / '.local-coder').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        LocalAdvisoryStore(tmp_path).query(advisory_id='CVE-2026-12345')


def test_model_tool_has_no_cache_import_or_network_execution_capability(tmp_path, monkeypatch):
    (tmp_path / 'requirements.txt').write_text('fixture==1.0.0\n')
    def forbidden(*args, **kwargs):
        pytest.fail('Dependency intelligence must remain offline and must not execute code')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    assert call(tmp_path).success
    assert not call(tmp_path, action='import', payload={}).success
    for values in [{'max_components': 0}, {'max_manifests': True}, {'target_path': '.env'}, {'action': 'query', 'package': 'fixture', 'ecosystem': 'other'}]:
        assert not call(tmp_path, **values).success


def test_inventory_limits_and_unsupported_lockfiles_are_explicit(tmp_path):
    (tmp_path / 'package-lock.json').write_text(json.dumps({'lockfileVersion': 3, 'packages': {'': {}, 'node_modules/alpha': {'version': '1.0.0'}, 'node_modules/beta': {'version': '2.0.0'}}}))
    result = call(tmp_path, max_components=1)
    assert result.success and len(result.data['components']) == 1
    assert result.data['coverage']['status'] == 'partial'
    (tmp_path / 'package-lock.json').write_text('{not valid JSON')
    broken = call(tmp_path)
    assert broken.success and broken.data['coverage']['status'] == 'partial'
    assert broken.data['coverage']['parse_errors'] == 1


def test_scoped_npm_names_and_legacy_nested_lock_versions_are_preserved(tmp_path):
    (tmp_path / 'package-lock.json').write_text(json.dumps({'lockfileVersion': 1, 'dependencies': {'@scope/alpha': {'version': '1.0.0', 'dependencies': {'nested': {'version': '2.0.0'}}}}}))
    report = DependencyInventory(tmp_path).scan()
    assert {(item['name'], item['version'], item['direct']) for item in report['components']} == {('@scope/alpha', '1.0.0', True), ('nested', '2.0.0', False)}

"""Range evaluation is conservative and retains legacy import behavior."""
import pytest

from local_coder.security_intelligence import LocalAdvisoryStore, evaluate_range


@pytest.mark.parametrize(('version', 'range_', 'expected'), [
    ('1.2.3', '>=1.0.0 <2.0.0', True), ('2.0.0', '<2.0.0', False),
    ('1.2.3', '^1.0.0', True), ('0.2.9', '^0.2.3', True),
    ('0.3.0', '^0.2.3', False), ('0.0.4', '^0.0.3', False),
    ('1.3.0', '~1.2.3', False), ('2.1.0', '<1.0.0 || >=2.0.0', True),
    ('1.2.3-beta.2', '>=1.2.3-beta.1 <1.2.3', True),
    ('1.2.3-beta.2', '<2.0.0', False),
    ('1.0.0+build', '=1.0.0', True), ('1.0.0', '1.x', None),
    ('1.0.0', '<=2', None), ('invalid', '<2.0.0', None),
])
def test_npm_ranges(version, range_, expected):
    assert evaluate_range('npm', version, range_) is expected


def test_python_pep440_adapter():
    pytest.importorskip('packaging')
    assert evaluate_range('pypi', '1.5', '~=1.4,!=1.6') is True
    assert evaluate_range('pypi', '2.0', '~=1.4') is False
    assert evaluate_range('pypi', '1.0rc2', '>=1.0rc1,<=1.0') is True
    assert evaluate_range('pypi', '1.0', '^1') is None


def osv(events, **extra):
    return {'id': 'PYSEC-2026-123', 'aliases': ['CVE-2026-12345', 'CVE-2026-12345'],
            'schema_version': '1.6.0', 'modified': '2026-10-26T00:00:00Z',
            'affected': [{'package': {'ecosystem': 'PyPI', 'name': 'Alpha_Pkg'},
                          'ranges': [{'type': 'ECOSYSTEM', 'events': events}]}], **extra}


def test_osv_intervals_aliases_withdrawal_and_provenance(tmp_path):
    pytest.importorskip('packaging')
    store = LocalAdvisoryStore(tmp_path)
    record = osv([{'introduced': '0'}, {'fixed': '1.2'}, {'introduced': '2'}, {'fixed': '3'}])
    store.import_records({'version': 2, 'advisories': [record]}, 'reviewed fixture')
    def query(version):
        return store.query(ecosystem='pypi', package='alpha.pkg', version=version)
    assert query('1.1')['matches'][0]['match_type'] == 'ecosystem_range'
    assert not query('1.2')['matches']
    assert query('2.0rc1')['matches'] == []
    assert query('2.1')['matches']
    assert not query('3')['matches']
    found = store.query(advisory_id='CVE-2026-12345')['matches'][0]
    assert found['aliases'] == ['CVE-2026-12345']
    assert found['schema_version'] == '1.6.0' and found['provenance']['format'] == 'OSV'
    store.import_records({'version': 2, 'advisories': [record | {'withdrawn': '2026-10-26T01:00:00Z'}]})
    assert not query('1.1')['matches']


@pytest.mark.parametrize(('events', 'version', 'matched'), [
    ([{'introduced': '0'}, {'last_affected': '1.2'}], '1.2', True),
    ([{'introduced': '0'}, {'last_affected': '1.2'}], '1.3', False),
    ([{'introduced': '0'}, {'limit': '1.2'}], '1.2', False),
    ([{'introduced': '0'}, {'limit': '*'}], '100', True),
])
def test_osv_inclusive_and_exclusive_bounds(tmp_path, events, version, matched):
    pytest.importorskip('packaging')
    store = LocalAdvisoryStore(tmp_path)
    store.import_records({'version': 2, 'advisories': [osv(events)]})
    assert bool(store.query(ecosystem='pypi', package='alpha-pkg', version=version)['matches']) is matched


def test_unsupported_ranges_and_malformed_intervals_are_unresolved(tmp_path):
    store = LocalAdvisoryStore(tmp_path)
    record = osv([{'fixed': '1.2'}])
    store.import_records({'version': 2, 'advisories': [record]})
    result = store.query(ecosystem='pypi', package='alpha-pkg', version='1.0')
    assert result['status'] == 'unresolved' and result['unresolved']
    assert result['no_match_is_not_safety_evidence']


def test_withdrawn_osv_without_affected_packages_tombstones_cached_records(tmp_path):
    store = LocalAdvisoryStore(tmp_path)
    store.import_records({'version': 2, 'advisories': [osv([], affected=[{
        'package': {'ecosystem': 'PyPI', 'name': 'alpha'}, 'versions': ['1.0']}])]})
    store.import_records({'version': 2, 'advisories': [{'id': 'PYSEC-2026-123', 'affected': [], 'withdrawn': '2026-10-26T00:00:00Z'}]})
    assert not store.query(ecosystem='pypi', package='alpha', version='1.0')['matches']


def test_invalid_withdrawal_timestamp_cannot_remove_matches(tmp_path):
    store = LocalAdvisoryStore(tmp_path)
    with pytest.raises(ValueError):
        store.import_records({'version': 2, 'advisories': [osv([], withdrawn='bad timestamp')]})


def test_single_version_osv_last_affected_interval_matches(tmp_path):
    store = LocalAdvisoryStore(tmp_path)
    store.import_records({'version': 2, 'advisories': [{
        'id': 'GHSA-abcd-1234-efgh', 'affected': [{
            'package': {'ecosystem': 'npm', 'name': 'alpha'},
            'ranges': [{'type': 'SEMVER', 'events': [{'introduced': '1.2.3'}, {'last_affected': '1.2.3'}]}]}]}]})
    result = store.query(ecosystem='npm', package='alpha', version='1.2.3')
    assert result['matches'] and not result['unresolved']
    assert not store.query(ecosystem='npm', package='alpha', version='1.2.4')['matches']


@pytest.mark.parametrize('version,expression', [
    ('3.0.0', '<2.0.0+..'), ('3.0.0+.', '<2.0.0'),
    ('3.0.0', '<2.0.0+a..b'), ('3.0.0', '<2.0.0+.a'),
])
def test_invalid_semver_build_is_unresolved(version, expression):
    assert evaluate_range('npm', version, expression) is None

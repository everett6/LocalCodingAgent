"""Public workbench routes connect each reliability workflow without inference."""
import json

from local_coder.remote import RemoteControlServer
from local_coder.security.ledger import FindingsLedger, LedgerEntry


def test_reliability_inspectors_never_construct_models_or_start_runtime(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    def forbidden(*args, **kwargs):
        raise AssertionError('Inspectors cannot start models')
    monkeypatch.setattr(server, '_coordinator', forbidden)
    monkeypatch.setattr(server.inference, 'start', forbidden)
    for route in ['/evaluations', '/findings', '/memory/retrievals', '/context/accounting', '/research/advisories/updates', '/inference/tuning/history', '/doctor']:
        code, result = server.handle('GET', route)
        assert code == 200, (route, result)
        assert isinstance(result, dict)
    assert server._jobs == {}


def test_model_evaluation_confirmation_and_public_offline_suite(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    def forbidden(*args, **kwargs):
        raise AssertionError('Missing confirmation cannot construct models')
    monkeypatch.setattr(server, '_coordinator', forbidden)
    code, overview = server.handle('GET', '/evaluations')
    suite_hash = overview['catalog']['suite_hash']
    code, report = server.handle('POST', '/evaluations/run', {'suite_hash': suite_hash})
    assert code == 200 and report['mode'] == 'offline_static'
    assert report['observed']['model_calls'] == 0
    assert report['quality']['cases'] > 0
    assert server.handle('POST', '/evaluations/model/run', {'suite_hash': suite_hash})[0] == 400
    assert server._job_lock.acquire(blocking=False)
    server._job_lock.release()


def test_workspace_job_blocks_repairs_model_runs_and_advisory_fetch(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('VALUE = 1\n')
    ledger = FindingsLedger.for_project(tmp_path)
    ledger.record(LedgerEntry(title='Fixture candidate', path='app.py', line=1, severity='LOW', rule='fixture'))
    server = RemoteControlServer(str(tmp_path))
    _, findings = server.handle('GET', '/findings')
    identity = findings['findings'][0]['id']
    _, catalog = server.handle('GET', '/evaluations')
    def forbidden(*args, **kwargs):
        raise AssertionError('An active task must prevent fetching or model construction')
    monkeypatch.setattr(server, '_coordinator', forbidden)
    monkeypatch.setattr('local_coder.dependency_updates_api._fetch_osv', forbidden)
    server._job_lock.acquire()
    try:
        for path, body in [
            ('/doctor/repair', {'action': 'ensure_private_project_state'}),
            ('/context/tokenizer', {'enabled': False}),
            ('/research/advisories/update', {'source': 'osv-record', 'advisory_id': 'CVE-2026-12345'}),
            ('/inference/tuning/start', {}),
            ('/evaluations/model/run', {'suite_hash': catalog['catalog']['suite_hash'], 'confirm_model_calls': True}),
            (f'/findings/{identity}/review', {}),
        ]:
            code, result = server.handle('POST', path, body)
            assert code in {400, 409}, (path, result)
    finally:
        server._job_lock.release()
    assert not server._jobs


def test_reliability_route_validation_is_secret_safe(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    value = 'private-invalid-fixture-value'
    for method, path, body in [
        ('GET', '/doctor?unknown=' + value, None),
        ('GET', '/context/accounting?unknown=' + value, None),
        ('GET', '/findings?unknown=' + value, None),
        ('POST', '/doctor/repair', {'action': value}),
        ('POST', '/evaluations/model/run', {'suite_hash': value, 'confirm_model_calls': True}),
    ]:
        code, result = server.handle(method, path, body)
        assert code == 400
        assert value not in json.dumps(result)


def test_new_workbench_state_directories_are_private(tmp_path):
    import os
    import stat
    import pytest
    if os.name == 'nt':
        pytest.skip('POSIX directory modes do not apply on Windows')
    RemoteControlServer(str(tmp_path))
    for path in [tmp_path / '.local-coder', tmp_path / '.local-coder/inference']:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_remediation_metrics_require_fresh_matching_regression_evidence(tmp_path, monkeypatch):
    from local_coder.security.evidence import EvidenceStore
    common = {'test_path': 'tests/test_case.py', 'test_digest': 'same-test', 'test_ids': ['case'], 'image_id': 'same-image', 'backend': 'docker'}
    before = {**common, 'status': 'failed', 'failed': 1, 'failure_kind': 'assertion'}
    after = {**common, 'status': 'passed', 'passed': 1}
    rows = [
        {'state': 'fixed', 'before': before, 'after': after},
        {'state': 'fixed', 'before': before, 'after': after, 'evidence_stale': True},
        {'state': 'fixed', 'before': before, 'after': {**after, 'test_digest': 'different-test'}},
        {'state': 'candidate', 'before': None, 'after': after},
    ]
    monkeypatch.setattr(EvidenceStore, 'list', lambda self: rows)
    server = RemoteControlServer(str(tmp_path))
    code, result = server.handle('GET', '/evaluations')
    assert code == 200
    assert result['project_remediation'] == {'available': True, 'successful_fixes': 1, 'reproduced_findings': 2, 'stale_records': 1}

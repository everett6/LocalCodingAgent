"""Canonical UI workflows use fake models and fake isolated runners only."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from local_coder.findings_api import FindingsAPI
from local_coder.security.evidence import EvidenceStore, FindingReview
from local_coder.security.ledger import FindingsLedger, LedgerEntry


class Sessions:
    def save(self, *args, **kwargs):
        return kwargs


class Host(FindingsAPI):
    def __init__(self, root):
        self.project_root = str(root)
        self.lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._context = threading.local()
        self._jobs = {}
        self._stopping = False
        self.sessions = Sessions()
        self.coordinator_calls = 0
        self.closed = False
        self.events = []
        self._init_findings_api()

    def _redact(self, value):
        return value

    def _event(self, event):
        self.events.append((self._context.session_id, event.event_type))

    def _coordinator(self):
        self.coordinator_calls += 1
        return self.coordinator

    def wait(self, job_id):
        job = self._jobs.get(job_id)
        if job:
            job['thread'].join(3)
            assert not job['thread'].is_alive()
        return self._findings_handle('GET', '/findings/jobs/' + job_id, {})[1]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr('local_coder.memory.goals.GoalLedger.revision', lambda self: 'source-v1')
    (tmp_path / 'app.py').write_text('safe = False')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_safe.py').write_text('def test_safe():\n    assert True\n')
    FindingsLedger.for_project(tmp_path).record(LedgerEntry('Local boundary', 'HIGH', 'app.py'))
    store = EvidenceStore(tmp_path)
    store.sync()
    identity = store.list()[0]['id']
    return Host(tmp_path), store, identity


def supported(store, identity):
    return store.review(identity, FindingReview(verdict='supported', input_trace='request to sink',
        prerequisites='input control', impact='unsafe behavior', counterevidence='guard checked'),
        reviewer_task='independent', revision='source-v1', finding=store.get(identity)['finding'])


def test_inspect_uses_canonical_ids_and_freshness_without_models(setup, monkeypatch):
    host, store, identity = setup
    supported(store, identity)
    monkeypatch.setattr('local_coder.memory.goals.GoalLedger.revision', lambda self: 'source-v2')
    status, response = host._findings_handle('GET', '/findings', {})
    assert status == 200 and response['findings'][0]['evidence_stale']
    assert len(identity) == 24 and response['jobs'] == []
    assert host._findings_handle('GET', '/findings/' + identity, {})[1]['review']['reviewer_task'] == 'independent'
    assert host.coordinator_calls == 0
    assert host._findings_handle('GET', '/findings/' + 'a' * 64, {})[0] == 404


def test_suppress_and_reopen_use_canonical_transitions_and_workspace_lock(setup):
    host, store, identity = setup
    path = '/findings/' + identity
    host._job_lock.acquire()
    assert host._findings_handle('POST', path + '/suppress', {'reason': 'Reviewed by operator'})[0] == 409
    host._job_lock.release()
    assert host._findings_handle('POST', path + '/suppress', {'reason': 'Reviewed by operator'})[1]['state'] == 'suppressed'
    assert host._findings_handle('POST', path + '/reopen', {})[1]['state'] == 'candidate'
    assert store.get(identity)['suppression'] == ''
    assert host._findings_handle('POST', path + '/confirm', {})[0] == 404
    assert host._findings_handle('POST', path + '/review', {'verdict': 'supported'})[0] == 400


def test_review_is_background_cancellable_and_closes_coordinator(setup):
    host, _, identity = setup
    entered = threading.Event()

    async def review(value):
        assert value == identity
        assert host._context.mode.value == 'cybersecurity'
        entered.set()
        await asyncio.Event().wait()

    async def close():
        host.closed = True

    host.coordinator = SimpleNamespace(review_security_finding=review, close=close)
    status, job = host._findings_handle('POST', f'/findings/{identity}/review', {})
    assert status == 202 and len(job['job_id']) == 32
    assert entered.wait(2)
    assert host._findings_handle('POST', f'/findings/{identity}/prepare', {})[0] == 409
    assert host._findings_handle('POST', f"/findings/jobs/{job['job_id']}/cancel", {})[0] == 200
    final = host.wait(job['job_id'])
    assert final['status'] == 'cancelled' and host.closed
    assert host._job_lock.acquire(blocking=False)
    host._job_lock.release()
    assert all(session == job['job_id'] for session, _ in host.events)


def test_prepare_returns_model_report_without_inventing_evidence(setup):
    host, store, identity = setup

    async def prepare(value):
        return 'Prepared tests/security_poc/test_boundary.py; not executed'

    async def close():
        host.closed = True

    host.coordinator = SimpleNamespace(prepare_security_regression=prepare, close=close)
    _, job = host._findings_handle('POST', f'/findings/{identity}/prepare', {})
    final = host.wait(job['job_id'])
    assert final['status'] == 'completed' and 'not executed' in final['result']
    assert store.get(identity)['state'] == 'candidate' and host.closed


def test_verification_uses_operator_fields_and_canonical_runner(setup, monkeypatch):
    host, store, identity = setup
    supported(store, identity)
    calls = []

    async def runner(root, test, image, **options):
        calls.append((test, image, options))
        return dict(status='failed', failed=1, passed=0, errors=0, tests=1, skipped=0,
                    image_id='sha256:' + 'a' * 64, backend='podman', test_ids=['tests/test_safe.py::test_safe'],
                    failure_kind='assertion', output='assertion failed')

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    body = dict(phase='before', test_path='tests/test_safe.py', image='installed:trusted', backend='podman', timeout=9)
    status, job = host._findings_handle('POST', f'/findings/{identity}/verify', body)
    assert status == 202
    final = host.wait(job['job_id'])
    assert final['status'] == 'completed' and final['result']['state'] == 'reproduced'
    assert calls == [('tests/test_safe.py', 'installed:trusted', {'timeout_seconds': 9, 'backend': 'podman'})]
    assert host.coordinator_calls == 0
    assert store.get(identity)['before']['image_id'].startswith('sha256:')


def test_strict_verification_routes_observed_controls_through_host(setup, monkeypatch):
    host, store, identity = setup
    supported(store, identity)
    calls = []

    async def controlled(self, finding, **options):
        calls.append((finding, options))
        return {'state': 'reproduced', 'strict_reproduced': True,
                'result': {'status': 'failed'}}

    monkeypatch.setattr(EvidenceStore, 'verify_controlled', controlled)
    body = dict(phase='before', test_path='tests/test_safe.py', image='installed:trusted',
                backend='podman', timeout=9, require_negative_controls=True,
                negative_control_ids=['tests/test_safe.py::test_control'])
    status, job = host._findings_handle('POST', f'/findings/{identity}/verify', body)
    assert status == 202
    assert host.wait(job['job_id'])['status'] == 'completed'
    assert calls[0][1]['negative_control_ids'] == ['tests/test_safe.py::test_control']
    assert host._findings_handle('POST', f'/findings/{identity}/verify', {
        **body, 'negative_control_ids': []})[0] == 400
    assert host.coordinator_calls == 0


@pytest.mark.parametrize('change', [dict(timeout=True), dict(timeout=0), dict(backend='shell'),
                                  dict(phase='fixed'), dict(image='-bad'), dict(test_path='../escape.py'), dict(test_path='app.py')])
def test_invalid_verify_is_rejected_before_start(setup, change):
    host, _, identity = setup
    body = dict(phase='before', test_path='tests/test_safe.py', image='trusted', backend='docker', timeout=60)
    body.update(change)
    assert host._findings_handle('POST', f'/findings/{identity}/verify', body)[0] == 400
    assert not host._jobs


def test_shutdown_rejects_new_work_and_history_is_bounded(setup):
    host, _, identity = setup
    host._finding_jobs = {f'{i:032x}': {'status': 'completed'} for i in range(40)}

    async def prepare(value):
        return 'No regression produced'

    async def close():
        pass

    host.coordinator = SimpleNamespace(prepare_security_regression=prepare, close=close)
    _, job = host._findings_handle('POST', f'/findings/{identity}/prepare', {})
    host.wait(job['job_id'])
    assert len(host._finding_jobs) == 40
    host._stopping = True
    assert host._findings_handle('POST', f'/findings/{identity}/prepare', {})[0] == 503
    assert not host._job_lock.locked()


@pytest.mark.parametrize('failure,expected', [('budget', 'budget_exhausted'), ('quota', 'quota_exhausted'), ('blocked', 'blocked')])
def test_model_control_failures_preserve_status_and_cleanup(setup, failure, expected):
    from local_coder.core.budgets import BudgetExceeded
    from local_coder.orchestrator.blocked import AgentBlocked
    from local_coder.providers.quota import ProviderQuotaExhausted
    from local_coder.types import AgentResponse, TaskStatus
    host, _, identity = setup

    async def review(value):
        if failure == 'budget':
            raise BudgetExceeded('request budget reached')
        if failure == 'quota':
            raise ProviderQuotaExhausted({'blocked': True, 'reason': 'daily quota'})
        raise AgentBlocked(AgentResponse(task_id='review-task', status=TaskStatus.BLOCKED, summary='Scope refused'))

    async def close():
        host.closed = True

    host.coordinator = SimpleNamespace(review_security_finding=review, close=close)
    _, job = host._findings_handle('POST', f'/findings/{identity}/review', {})
    final = host.wait(job['job_id'])
    assert final['status'] == expected and host.closed
    if failure == 'quota':
        assert final['quota_notice']['blocked']


def test_cancel_during_cleanup_keeps_slot_until_coordinator_is_closed(setup):
    host, _, identity = setup
    cleaning = threading.Event()
    released = threading.Event()

    async def review(value):
        return {'state': 'candidate'}

    async def close():
        cleaning.set()
        while not released.is_set():
            await asyncio.sleep(.01)
        host.closed = True

    host.coordinator = SimpleNamespace(review_security_finding=review, close=close)
    _, job = host._findings_handle('POST', f'/findings/{identity}/review', {})
    assert cleaning.wait(2)
    endpoint = f"/findings/jobs/{job['job_id']}/cancel"
    host._findings_handle('POST', endpoint, {})
    host._findings_handle('POST', endpoint, {})
    assert host._job_lock.locked()
    released.set()
    assert host.wait(job['job_id'])['status'] == 'cancelled'
    assert host.closed and not host._job_lock.locked()


def test_infrastructure_failure_is_not_a_successful_verification(setup, monkeypatch):
    host, store, identity = setup
    supported(store, identity)

    async def runner(*args, **kwargs):
        return dict(status='unavailable', failed=0, passed=0, errors=0, tests=0, skipped=0,
                    image_id='', backend='docker', test_ids=[], failure_kind='', error='image unavailable')

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    body = dict(phase='before', test_path='tests/test_safe.py', image='missing', backend='docker', timeout=5)
    _, job = host._findings_handle('POST', f'/findings/{identity}/verify', body)
    assert host.wait(job['job_id'])['status'] == 'failed'
    assert store.get(identity)['state'] == 'reviewed' and store.get(identity)['before'] is None

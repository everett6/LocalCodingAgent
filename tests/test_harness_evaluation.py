import asyncio
import json
from types import SimpleNamespace

import pytest

from local_coder.core.budgets import BudgetExceeded, BudgetLedger
from local_coder.evaluation_api import EvaluationAPI
from local_coder.harness.evaluation import CASES, EvaluationStore, detect, make_model_evaluator, run_offline, score_results, suite_hash


def test_fixed_suite_is_deterministic_and_has_negative_controls():
    first, second = run_offline(), run_offline()
    assert first['suite_hash'] == second['suite_hash'] == suite_hash()
    assert first['quality'] == second['quality']
    assert first['quality']['passed'] == len(CASES)
    assert first['quality']['false_positives'] == first['quality']['false_negatives'] == 0
    assert first['observed']['model_calls'] == first['observed']['total_tokens'] == 0
    assert all(not detect(c.source) for c in CASES if c.kind in {'fixed', 'harmless'})


def test_scorer_reports_false_positives_and_false_negatives():
    labels = {case.id: list(case.expected) for case in CASES}
    labels['eval-harmless'] = ['dynamic_eval']
    labels['eval-vulnerable'] = []
    result = score_results(labels, expected_suite_hash=suite_hash())
    assert result['false_positives'] == result['false_negatives'] == 1
    assert result['precision'] == result['recall'] == pytest.approx(2 / 3)
    with pytest.raises(ValueError, match='hash mismatch'):
        score_results(labels, expected_suite_hash='other')
    labels['total_tokens'] = 0
    with pytest.raises(ValueError):
        score_results(labels, expected_suite_hash=suite_hash())


@pytest.mark.parametrize('kwargs', [{'max_cases': 1}, {'max_cases': True}, {'timeout_seconds': 0}, {'timeout_seconds': 61}, {'expected_suite_hash': 'mismatch'}])
def test_offline_limits(kwargs):
    with pytest.raises(ValueError):
        run_offline(**kwargs)


def test_inspected_source_is_never_executed(tmp_path):
    marker = tmp_path / 'executed'
    assert detect(f"open({str(marker)!r}, 'w').write('bad')") == []
    assert not marker.exists()


def test_private_history_and_routes(tmp_path):
    host = EvaluationAPI()
    host.project_root = tmp_path
    assert host._evaluation_get('/elsewhere', {}) == (False, None)
    handled, report = host._evaluation_post('/evaluations/run', {'suite_hash': suite_hash()})
    assert handled
    assert host._evaluation_get('/evaluations/' + report['id'], {}) == (True, report)
    path = tmp_path / EvaluationStore.relative
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError):
        host._evaluation_post('/evaluations/run', {'total_tokens': 0})
    with pytest.raises(ValueError):
        host._evaluation_get('/evaluations/../../secret', {})
    raw = json.loads(path.read_text())
    raw[0]['secret'] = 'PRIVATE_SECRET_VALUE'
    raw[0]['quality']['results'][0]['content'] = 'PRIVATE_SECRET_VALUE'
    path.write_text(json.dumps(raw))
    assert 'PRIVATE_SECRET_VALUE' not in json.dumps(host._evaluation_get('/evaluations', {}))


def test_store_rejects_symlink(tmp_path):
    store = EvaluationStore(tmp_path)
    target = tmp_path / '.local-coder'
    target.mkdir()
    (target / 'evaluations').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        store.run()


class FixtureModel:
    config = SimpleNamespace(model_id='local-fixture', input_cost_per_million=1.0, output_cost_per_million=2.0, request_cost_usd=0)
    async def generate(self, messages, **kwargs):
        return SimpleNamespace(content=json.dumps(detect(messages[-1].content)), prompt_tokens=10, completion_tokens=3)


def test_paired_adapter_calls_model_and_measures_usage():
    ledger = BudgetLedger()
    overlays = []
    def factory(overlay):
        overlays.append(overlay)
        return FixtureModel()
    evaluator = make_model_evaluator(factory, ledger=ledger)
    result = asyncio.run(evaluator({'temperature': 0}))
    assert result['success']
    assert result['measurements'] == {'cases': len(CASES), 'passed': len(CASES), 'total_tokens': len(CASES) * 13, 'model_calls': len(CASES)}
    assert result['report']['observed']['cost_usd'] == pytest.approx(len(CASES) * 16 / 1_000_000)
    assert ledger.status()['requests'] == len(CASES)
    assert overlays == [{'temperature': 0}]


def test_model_adapter_enforces_shared_admission():
    evaluator = make_model_evaluator(lambda overlay: FixtureModel(), ledger=BudgetLedger(max_requests=1))
    with pytest.raises(BudgetExceeded):
        asyncio.run(evaluator({}))


def test_model_adapter_does_not_accept_text_usage_claims():
    class UnknownUsage(FixtureModel):
        async def generate(self, messages, **kwargs):
            return SimpleNamespace(content=json.dumps(detect(messages[-1].content)), prompt_tokens=0, completion_tokens=0)
    evaluator = make_model_evaluator(lambda overlay: UnknownUsage(), ledger=BudgetLedger())
    with pytest.raises(ValueError, match='usage unavailable'):
        asyncio.run(evaluator({}))


def test_history_is_bounded(tmp_path, monkeypatch):
    import local_coder.harness.evaluation as module
    monkeypatch.setattr(module, 'MAX_HISTORY', 2)
    store = EvaluationStore(tmp_path)
    for _ in range(3):
        store.run()
    assert len(store.history()) == 2


def test_model_adapter_reports_missing_cost_without_inventing_zero():
    class UnknownPrice(FixtureModel):
        config = SimpleNamespace(model_id='local-fixture')
    result = asyncio.run(make_model_evaluator(lambda overlay: UnknownPrice(), ledger=BudgetLedger())({}))
    assert result['report']['observed']['cost_usd'] is None
    assert result['report']['missing_metrics'] == ['cost_usd']


def test_model_adapter_honors_quota_and_cancellation():
    class Quota:
        def admit(self, config):
            raise RuntimeError('quota denied')
    ledger = BudgetLedger()
    evaluator = make_model_evaluator(lambda overlay: FixtureModel(), ledger=ledger, quota_guard=Quota())
    with pytest.raises(RuntimeError, match='quota denied'):
        asyncio.run(evaluator({}))
    assert ledger.status()['requests'] == 0

    class Waiting(FixtureModel):
        async def generate(self, messages, **kwargs):
            await asyncio.Event().wait()
    async def cancel():
        task = asyncio.create_task(make_model_evaluator(lambda overlay: Waiting(), ledger=ledger)({}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert ledger.status()['entries'][0]['status'] == 'failed'


def test_invalid_labels_and_history_suite_mismatch(tmp_path):
    labels = {case.id: list(case.expected) for case in CASES}
    labels[CASES[0].id] = [{'tokens': 0}]
    with pytest.raises(ValueError):
        score_results(labels, expected_suite_hash=suite_hash())
    store = EvaluationStore(tmp_path)
    store.run()
    path = tmp_path / store.relative
    content = json.loads(path.read_text())
    content[0]['suite_hash'] = 'different'
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match='suite mismatch'):
        store.history()


def test_paired_adapter_integrates_with_revision_gate(tmp_path):
    from local_coder.harness.evolution import EvolutionStore
    from local_coder.harness.improvement import improvement_cycle

    class Improving(FixtureModel):
        def __init__(self, improved):
            self.improved = improved
        async def generate(self, messages, **kwargs):
            response = await super().generate(messages, **kwargs)
            if self.improved:
                response.completion_tokens = 2
            return response
    store = EvolutionStore(tmp_path)
    evaluator = make_model_evaluator(lambda overlay: Improving(overlay.get('max_strategy_attempts') == 1), ledger=BudgetLedger())
    result = asyncio.run(improvement_cycle(store, [{'max_strategy_attempts': 1}], evaluator))
    assert result['revisions'][0]['state'] == 'validated'
    assert result['revisions'][0]['measurements']['total_tokens'] < result['baseline']['total_tokens']
    assert result['activated'] is False
    assert store.active_overlay() == {}


class EvaluationHost(EvaluationAPI):
    def __init__(self, root, *, cap=None, cloud=False, model_type=FixtureModel):
        import threading
        from local_coder.types import ModelConfig, ProjectConfig
        self.project_root = root
        self.lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._jobs = {}
        self._stopping = False
        self._context = threading.local()
        self.sessions = SimpleNamespace(save=lambda *args, **kwargs: None)
        self._init_evaluation_api()
        self.constructed = self.closed = 0
        self.ledger = BudgetLedger(max_cost_usd=cap)
        config = ProjectConfig(models={'fixture': ModelConfig(name='fixture', model_id='synthetic',
            base_url='https://provider.invalid/v1' if cloud else 'http://localhost:1234',
            input_cost_per_million=1 if cloud else 0, output_cost_per_million=1 if cloud else 0)})
        host = self
        class Manager:
            quota_guard = None
            async def get_model(self, role, name):
                host.constructed += 1
                backend = model_type()
                backend.config = self.config.models[name]
                return BudgetedModel(backend, host.ledger)
            async def close_all(self):
                host.closed += 1
        from local_coder.core.budgets import BudgetedModel
        self.manager = Manager()
        self.manager.config = config

    def _coordinator(self):
        return SimpleNamespace(model_manager=self.manager, budget=self.ledger)

    def _event(self, event):
        pass

    def _redact(self, value):
        return value

    def run_model(self):
        handled, row = self._evaluation_post('/evaluations/model/run', {'suite_hash': suite_hash(), 'confirm_model_calls': True})
        assert handled
        with self.lock:
            job = self._jobs.get(row['job_id'])
        if job:
            job['thread'].join(timeout=5)
            assert not job['thread'].is_alive()
        return self._evaluation_get('/evaluations/jobs/' + row['job_id'], {})[1]


def test_operator_model_job_persists_actual_usage_without_double_budget(tmp_path):
    host = EvaluationHost(tmp_path)
    row = host.run_model()
    assert row['status'] == 'completed', row
    assert row['result']['mode'] == 'model'
    assert row['result']['quality']['passed'] == len(CASES)
    assert row['result']['observed']['total_tokens'] == 13 * len(CASES)
    assert row['result']['model']['model_id'] == 'synthetic'
    assert host.ledger.status()['requests'] == len(CASES)
    assert host.constructed == host.closed == 1
    assert EvaluationStore(tmp_path).history()[0] == row['result']
    assert not host._job_lock.locked()


def test_cloud_rejected_before_backend_construction_without_cap(tmp_path):
    host = EvaluationHost(tmp_path, cloud=True)
    assert host.run_model()['status'] == 'failed'
    assert host.constructed == 0 and host.closed == 1
    assert host.ledger.status()['requests'] == 0
    assert not EvaluationStore(tmp_path).history()


def test_cloud_explicit_cap_uses_selected_model_only(tmp_path):
    host = EvaluationHost(tmp_path, cloud=True, cap=0.01)
    assert host.run_model()['status'] == 'completed'
    assert host.manager.config.routing.fallbacks == {}
    assert host.closed == 1


def test_operator_confirmation_and_suite_required(tmp_path):
    host = EvaluationHost(tmp_path)
    for body in [{'suite_hash': suite_hash()}, {'suite_hash': 'wrong', 'confirm_model_calls': True},
                 {'suite_hash': suite_hash(), 'confirm_model_calls': True, 'results': {}}]:
        with pytest.raises(ValueError):
            host._evaluation_post('/evaluations/model/run', body)
    assert host.constructed == 0


def test_model_run_can_report_unknown_usage_honestly(tmp_path):
    class MissingUsage(FixtureModel):
        async def generate(self, messages, **kwargs):
            return SimpleNamespace(content=json.dumps(detect(messages[-1].content)), prompt_tokens=0, completion_tokens=0)
    host = EvaluationHost(tmp_path, model_type=MissingUsage)
    row = host.run_model()
    assert row['status'] == 'completed'
    assert row['result']['observed']['total_tokens'] is None
    assert row['result']['missing_metrics'] == ['total_tokens', 'cost_usd']


def test_model_job_cancel_closes_coordinator_and_releases_slot(tmp_path):
    import threading
    entered = threading.Event()
    class Waiting(FixtureModel):
        async def generate(self, messages, **kwargs):
            entered.set()
            await asyncio.Event().wait()
    host = EvaluationHost(tmp_path, model_type=Waiting)
    _, row = host._evaluation_post('/evaluations/model/run', {'suite_hash': suite_hash(), 'confirm_model_calls': True})
    assert entered.wait(2)
    with host.lock:
        job = host._jobs[row['job_id']]
    _, cancelling = host._evaluation_post('/evaluations/jobs/' + row['job_id'] + '/cancel', {})
    assert cancelling['status'] == 'cancelling'
    job['thread'].join(5)
    assert host._evaluation_get('/evaluations/jobs/' + row['job_id'], {})[1]['status'] == 'cancelled'
    assert host.closed == 1 and not host._job_lock.locked()
    assert not EvaluationStore(tmp_path).history()


def test_model_history_rejects_invented_usage(tmp_path):
    host = EvaluationHost(tmp_path)
    host.run_model()
    path = tmp_path / EvaluationStore.relative
    history = json.loads(path.read_text())
    history[0]['observed']['total_tokens'] = -1
    path.write_text(json.dumps(history))
    with pytest.raises(ValueError):
        EvaluationStore(tmp_path).history()


def test_model_quota_exhaustion_closes_and_never_calls_backend(tmp_path):
    from local_coder.providers.quota import ProviderQuotaExhausted
    class Denied:
        def admit(self, config):
            raise ProviderQuotaExhausted({'code': 'free_daily_requests_exhausted'})
    host = EvaluationHost(tmp_path)
    host.manager.quota_guard = Denied()
    row = host.run_model()
    assert row['status'] == 'quota_exhausted'
    assert row['quota_notice']['code'] == 'free_daily_requests_exhausted'
    assert host.ledger.status()['requests'] == 0
    assert host.closed == 1 and not host._job_lock.locked()
    assert not EvaluationStore(tmp_path).history()


def test_unknown_pricing_requires_cap_even_for_local_endpoint(tmp_path):
    host = EvaluationHost(tmp_path)
    host.manager.config.models['fixture'].input_cost_per_million = None
    assert host.run_model()['status'] == 'failed'
    assert host.constructed == 0


def test_model_job_busy_slot_and_budget_exhaustion(tmp_path):
    host = EvaluationHost(tmp_path, cloud=True, cap=0.0000001)
    row = host.run_model()
    assert row['status'] == 'budget_exhausted'
    assert host.closed == 1
    host._job_lock.acquire()
    try:
        with pytest.raises(ValueError, match='already running'):
            host._evaluation_post('/evaluations/model/run', {'suite_hash': suite_hash(), 'confirm_model_calls': True})
    finally:
        host._job_lock.release()


def test_model_invalid_response_closes_coordinator(tmp_path):
    class Invalid(FixtureModel):
        async def generate(self, messages, **kwargs):
            return SimpleNamespace(content='not JSON', prompt_tokens=10, completion_tokens=1)
    host = EvaluationHost(tmp_path, model_type=Invalid)
    assert host.run_model()['status'] == 'failed'
    assert host.closed == 1 and not host._job_lock.locked()
    assert not EvaluationStore(tmp_path).history()

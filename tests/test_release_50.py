"""Release claims require held-out, measured and reviewable evidence."""
import asyncio
import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_coder.core.budgets import BudgetExceeded, BudgetLedger
from local_coder.harness import evaluation
from local_coder.harness.evolution import EvolutionStore
from local_coder.harness.improvement import improvement_cycle

ROOT = Path(__file__).resolve().parents[1]


def benchmark():
    assert hasattr(evaluation, 'heldout_catalog'), 'Held-out coding/bug/security suite is missing'
    return evaluation


def test_heldout_catalog_is_versioned_and_separate_from_adaptation():
    module = benchmark()
    catalog = module.heldout_catalog()
    assert {row['domain'] for row in catalog['cases']} == {'coding', 'security', 'bug'}
    assert all(any(row['control'] and row['domain'] == domain for row in catalog['cases'])
               for domain in ('coding', 'security', 'bug'))
    assert len(catalog['suite_hash']) == 64 and catalog['suite_hash'] != evaluation.suite_hash()
    assert catalog['purpose'] == 'release_holdout'
    assert all('source' not in row and 'expected' not in row for row in catalog['cases'])
    assert catalog['quality_claim'] == 'unverified_without_model_runs'


class HoldoutModel:
    config = SimpleNamespace(model_id='test-double', base_url='http://127.0.0.1', backend='openai_compatible', input_cost_per_million=0,
                             output_cost_per_million=0, request_cost_usd=0)
    async def generate(self, messages, **kwargs):
        task = messages[-1].content
        if 'normalize_name' in task:
            answer = {'source': 'def normalize_name(value):\n    return value.strip().lower()\n'}
        elif 'clamp' in task:
            answer = {'source': 'def clamp(value, low, high):\n    return min(max(value, low), high)\n'}
        else:
            answer = {'findings': ['unsafe_yaml'] if 'Loader=yaml.Loader' in task else []}
        return SimpleNamespace(content=json.dumps(answer), prompt_tokens=30, completion_tokens=25)


def test_holdout_measures_fake_transport_without_claiming_real_model_quality(tmp_path):
    module = benchmark()
    ledger = BudgetLedger(max_requests=30)
    evaluator = module.make_heldout_evaluator(lambda _: HoldoutModel(), ledger=ledger,
                  expected_suite_hash=module.heldout_catalog()['suite_hash'])
    result = asyncio.run(evaluator({}))
    assert result['quality']['passed'] == result['quality']['cases'] == 6
    assert result['quality']['false_positives'] == 0
    assert result['observed']['cost_usd'] == 0
    assert ledger.status()['requests'] == 6
    assert result['mode'] == 'model' and result['purpose'] == 'release_holdout'
    assert result['quality_claim'] == 'measured_transport_only_unverified_model_identity'
    with pytest.raises(ValueError, match='held.out'):
        asyncio.run(improvement_cycle(EvolutionStore(tmp_path), [{'strategy': 'small_steps'}], evaluator))


def test_heldout_rejects_tampering_budget_exhaustion_and_unsafe_model_code():
    module = benchmark()
    with pytest.raises(ValueError, match='hash'):
        module.make_heldout_evaluator(lambda _: HoldoutModel(), ledger=BudgetLedger(), expected_suite_hash='wrong')
    run = module.make_heldout_evaluator(lambda _: HoldoutModel(), ledger=BudgetLedger(max_requests=1))
    with pytest.raises(BudgetExceeded):
        asyncio.run(run({}))
    class Unsafe(HoldoutModel):
        async def generate(self, messages, **kwargs):
            return SimpleNamespace(content=json.dumps({'source': "__import__('os').remove('never')"}), prompt_tokens=1, completion_tokens=1)
    result = asyncio.run(module.make_heldout_evaluator(lambda _: Unsafe(), ledger=BudgetLedger())({}))
    assert result['quality']['passed'] == 0
    assert result['observed']['response_errors'] == 6


def profile_report(**changes):
    result = {'suite_hash': evaluation.suite_hash(), 'mode': 'model',
              'benchmark': 'instruction_profile_fixed_suite', 'purpose': 'profile_regression',
              'quality': {'cases': 9, 'passed': 9, 'false_positives': 0},
              'observed': {'model_calls': 9, 'total_tokens': 900, 'cost_usd': 0.01,
                           'elapsed_seconds': 1.0, 'tool_failures': 0, 'response_errors': 0}}
    for key, value in changes.items():
        result['quality' if key in result['quality'] else 'observed'][key] = value
    return result


def test_profile_gate_checks_all_observations_and_unknowns():
    import local_coder.harness.improvement as module
    assert hasattr(module, 'compare_profile_reports'), 'Measured profile gate is missing'
    base = profile_report()
    assert module.compare_profile_reports(base, profile_report(total_tokens=800))['passed']
    for change in ({'passed': 8}, {'false_positives': 1}, {'cost_usd': 0.02},
                   {'elapsed_seconds': 2}, {'tool_failures': 1}, {'response_errors': 1}, {'cost_usd': None}):
        assert not module.compare_profile_reports(base, profile_report(total_tokens=800, **change))['passed'], change
    wrong = profile_report(total_tokens=800)
    wrong['suite_hash'] = 'other'
    assert not module.compare_profile_reports(base, wrong)['passed']
    heldout = copy.deepcopy(base)
    heldout['purpose'] = 'release_holdout'
    assert not module.compare_profile_reports(base, heldout)['passed']


def test_readiness_maps_exact_spec_and_does_not_promote_source_or_unknown_gates():
    assert importlib.util.find_spec('local_coder.release_readiness'), 'Readiness module is missing'
    module = importlib.import_module('local_coder.release_readiness')
    report = module.readiness_report(ROOT)
    spec = json.loads((ROOT / 'docs/roadmap/clin-50-spec.json').read_text())
    assert [(row['id'], row['title'], row['acceptance']) for row in report['items']] == [
        (row['id'], row['title'], row['acceptance']) for row in spec['items']]
    assert not report['ready'] and len(report['items']) == 50
    assert all('evidence' in row and 'missing_gates' in row and row['status'] != 'implemented' for row in report['items'])
    assert all(row['status'] == 'unverified' for row in report['native_runs'].values())
    assert {'brand_clearance', 'update_signing', 'support', 'licenses', 'privacy', 'key_storage', 'rollback'} <= set(report['commercial'])
    assert report['commercial']['licenses']['root_license']['status'] == 'missing'
    assert 'brand_clearance' in report['missing_external_gates']


def test_readiness_rejects_stale_or_escaping_attestation(tmp_path):
    assert importlib.util.find_spec('local_coder.release_readiness'), 'Readiness module is missing'
    module = importlib.import_module('local_coder.release_readiness')
    directory = tmp_path / '.local-coder/release'
    directory.mkdir(parents=True)
    (directory / 'evidence.json').write_text(json.dumps({'revision': 'wrong', 'commercial': {'brand_clearance': {
        'status': 'verified', 'path': '../outside', 'sha256': '0' * 64, 'reviewed_by': 'someone'}}}))
    report = module.readiness_report(tmp_path)
    assert not report['ready'] and report['commercial']['brand_clearance']['status'] == 'unverified'
    assert len(report['items']) == 50


def test_actual_profile_adapter_requires_known_cost_and_rejects_latency_only_wins(tmp_path):
    from local_coder.harness.improvement import compare_profile_reports
    from tests.test_harness_evaluation import FixtureModel
    base = asyncio.run(evaluation.make_model_evaluator(lambda _: FixtureModel(), ledger=BudgetLedger(), render_profile=True)({}))
    assert base['report']['purpose'] == 'profile_regression'
    equal = copy.deepcopy(base['report'])
    equal['observed']['elapsed_seconds'] = base['report']['observed']['elapsed_seconds'] / 2
    assert not compare_profile_reports(base['report'], equal)['passed']
    class UnknownCost(FixtureModel):
        config = SimpleNamespace(model_id='unknown-cost')
    unknown = asyncio.run(evaluation.make_model_evaluator(lambda _: UnknownCost(), ledger=BudgetLedger(), render_profile=True)({}))
    assert not compare_profile_reports(base['report'], unknown['report'])['passed']


def test_readiness_checks_actual_license_and_attestation_file_hashes(tmp_path, monkeypatch):
    from local_coder import release_readiness as module
    monkeypatch.setattr(module, '_revision', lambda _: 'a' * 40)
    directory = tmp_path / '.local-coder/release'
    directory.mkdir(parents=True)
    proof = tmp_path / 'brand-review.txt'
    proof.write_text('Independent supplied review, no legal conclusion generated by the agent')
    import hashlib
    attestation = {'status': 'verified', 'revision': 'a' * 40, 'reviewed_by': 'Release approver',
                   'evidence': [{'path': 'brand-review.txt', 'sha256': hashlib.sha256(proof.read_bytes()).hexdigest()}]}
    (directory / 'evidence.json').write_text(json.dumps({'commercial': {'brand_clearance': attestation}}))
    assert module.readiness_report(tmp_path)['commercial']['brand_clearance']['status'] == 'attested'
    proof.write_text('Changed after review')
    report = module.readiness_report(tmp_path)
    assert report['commercial']['brand_clearance']['status'] == 'unverified'
    assert report['commercial']['licenses']['root_license']['status'] == 'missing'


def test_publication_verifier_refuses_unsigned_or_mismatched_artifacts(tmp_path, monkeypatch):
    script = ROOT / 'desktop/verify-release.py'
    assert script.exists(), 'Publication evidence/signature verifier is missing'
    import runpy
    verify = runpy.run_path(str(script))['verify_release']
    approval = tmp_path / 'approval'
    assets = tmp_path / 'assets'
    approval.mkdir()
    assets.mkdir()
    monkeypatch.delenv('RELEASE_SIGNING_PUBLIC_KEY', raising=False)
    monkeypatch.delenv('RELEASE_SIGNING_FINGERPRINT', raising=False)
    with pytest.raises(ValueError, match='signing'):
        verify(tmp_path, approval, assets)
    monkeypatch.setenv('RELEASE_SIGNING_PUBLIC_KEY', '-----BEGIN PGP PRIVATE KEY BLOCK-----')
    monkeypatch.setenv('RELEASE_SIGNING_FINGERPRINT', 'A' * 40)
    with pytest.raises(ValueError, match='public'):
        verify(tmp_path, approval, assets)


def test_operator_heldout_job_requires_confirmation_and_records_separate_history(tmp_path):
    from tests.test_harness_evaluation import EvaluationHost
    host = EvaluationHost(tmp_path, model_type=HoldoutModel)
    identity = benchmark().heldout_catalog()['suite_hash']
    with pytest.raises(ValueError):
        host._evaluation_post('/evaluations/heldout/run', {'suite_hash': identity})
    handled, row = host._evaluation_post('/evaluations/heldout/run', {'suite_hash': identity, 'confirm_model_calls': True})
    assert handled
    with host.lock:
        job = host._jobs.get(row['job_id'])
    if job:
        job['thread'].join(5)
        assert not job['thread'].is_alive()
    report = host._evaluation_get('/evaluations/jobs/' + row['job_id'], {})[1]
    assert report['status'] == 'completed', report
    assert report['result']['purpose'] == 'release_holdout'
    assert report['result']['quality']['passed'] == 6
    _, index = host._evaluation_get('/evaluations', {})
    assert len(index['release_holdout_history']) == 1
    assert not index['history']
    assert host.ledger.status()['requests'] == 6 and host.closed == 1


def test_signing_verifier_checks_manifest_digest_and_rejects_invalid_public_key(tmp_path, monkeypatch):
    import hashlib
    import runpy
    import subprocess
    verify = runpy.run_path(str(ROOT / 'desktop/verify-release.py'))['verify_release']
    approval, assets = tmp_path / 'approval', tmp_path / 'assets'
    approval.mkdir()
    assets.mkdir()
    evidence = b'{}'
    artifact = b'local test artifact, never published'
    (approval / 'evidence.json').write_bytes(evidence)
    (approval / 'ide-linux-x64.json.asc').write_text('invalid signature')
    (assets / 'local-coder-ide-linux-x64.tar.gz').write_bytes(artifact)
    revision = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], check=True, text=True, capture_output=True).stdout.strip()
    manifest = {'tag': 'ide-vscode-test', 'platform': 'linux', 'arch': 'x64',
                'asset': 'local-coder-ide-linux-x64.tar.gz', 'revision': revision,
                'readiness_evidence_sha256': hashlib.sha256(evidence).hexdigest(),
                'sha256': '0' * 64}
    (assets / 'ide-linux-x64.json').write_text(json.dumps(manifest))
    (approval / 'ide-linux-x64.json').write_text(json.dumps(manifest))
    monkeypatch.setenv('RELEASE_SIGNING_PUBLIC_KEY', '-----BEGIN PGP PUBLIC KEY BLOCK-----\nInvalid public verification fixture')
    monkeypatch.setenv('RELEASE_SIGNING_FINGERPRINT', 'A' * 40)
    monkeypatch.setenv('GITHUB_SHA', revision)
    with pytest.raises(ValueError, match='digest mismatch'):
        verify(ROOT, approval, assets)
    manifest['sha256'] = hashlib.sha256(artifact).hexdigest()
    (assets / 'ide-linux-x64.json').write_text(json.dumps(manifest))
    (approval / 'ide-linux-x64.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='signature verification failed'):
        verify(ROOT, approval, assets)


def test_heldout_history_detects_tampering_and_stays_outside_adaptation(tmp_path):
    from local_coder.harness.evaluation import HeldoutEvaluationStore
    report = asyncio.run(benchmark().make_heldout_evaluator(lambda _: HoldoutModel(), ledger=BudgetLedger())({}))
    store = HeldoutEvaluationStore(tmp_path)
    store.record_model(report)
    assert store.history()[0]['quality']['passed'] == 6
    path = tmp_path / store.relative
    rows = json.loads(path.read_text())
    rows[0]['observed']['model_calls'] = 0
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match='measurements'):
        store.history()


def test_direct_holdout_adapter_requires_spending_admission_before_requests():
    class Metered(HoldoutModel):
        config = SimpleNamespace(model_id='metered-fixture', base_url='https://provider.invalid',
            backend='openai_compatible', input_cost_per_million=1, output_cost_per_million=1, request_cost_usd=0)
    run = benchmark().make_heldout_evaluator(lambda _: Metered(), ledger=BudgetLedger())
    with pytest.raises(ValueError, match='dollar cap'):
        asyncio.run(run({}))


def test_direct_profile_revision_validation_requires_paired_observations(tmp_path):
    import local_coder.harness.improvement as module
    assert hasattr(module, 'validate_profile_revision'), 'Direct profile validation must use the paired gate'
    store = EvolutionStore(tmp_path)
    revision = store.propose('loop_plan', {'strategy': 'small_steps'})
    async def evaluator(overlay):
        report = profile_report(total_tokens=800 if overlay else 900, cost_usd=0.02 if overlay else 0.01)
        return {'success': True, 'measurements': {'cases': 9, 'passed': 9, 'total_tokens': report['observed']['total_tokens'], 'model_calls': 9}, 'report': report}
    result = asyncio.run(module.validate_profile_revision(store, revision, evaluator))
    assert not result['success']
    assert result['gate']['reasons'] == ['cost_usd_regression']
    assert not store.active_revisions()


def test_harness_status_exposes_readiness_without_inventing_success(tmp_path, monkeypatch):
    from tests.test_harness_api import FixtureAPI
    monkeypatch.setenv('LOCAL_CODER_USER_CONFIG', str(tmp_path / 'no-user-config.yaml'))
    status = FixtureAPI(tmp_path)._harness_get('/harness', {})
    assert 'readiness' in status, 'Readiness must be available through the authenticated harness host'
    assert status['readiness']['ready'] is False
    assert len(status['readiness']['items']) == 50

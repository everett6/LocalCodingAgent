import asyncio
import json
import subprocess

import pytest

from local_coder.memory.goals import GoalLedger
from local_coder.security.evidence import EvidenceStore, FindingReview
from local_coder.security.ledger import FindingsLedger, LedgerEntry


def setup(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.test', 'commit', '--allow-empty', '-m', 'init'], check=True, capture_output=True)
    (tmp_path / 'app.py').write_text('safe = False')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_safe.py').write_text('def test_safe():\n    assert True\n')
    FindingsLedger.for_project(tmp_path).record(LedgerEntry('Boundary issue', 'HIGH', 'app.py', confidence=10, status='confirmed'))
    store = EvidenceStore(tmp_path)
    store.sync()
    identity = store.list()[0]['id']
    return store, identity


def review(store, identity, verdict='supported'):
    return store.review(identity, FindingReview(verdict=verdict, input_trace='request to sink',
        prerequisites='local input', impact='incorrect access', counterevidence='guard inspected'),
        reviewer_task='independent-task', revision=GoalLedger(store.root).revision(), finding=store.get(identity)['finding'])


def outcome(status):
    return dict(status=status, failed=int(status == 'failed'), passed=int(status == 'passed'),
                errors=0, tests=1, skipped=0, image_id='sha256:' + 'a' * 64, backend='docker',
                test_ids=['tests/test_safe.py::test_safe'], output='evidence', failure_kind='assertion')


def run(store, identity, phase='before'):
    return asyncio.run(store.verify(identity, phase=phase, test_path='tests/test_safe.py', image='trusted'))


def test_model_claims_do_not_confirm_and_identity_survives_restart(tmp_path):
    store, identity = setup(tmp_path)
    assert store.get(identity)['state'] == 'candidate'
    restored = EvidenceStore(tmp_path)
    restored.sync()
    assert len(restored.list()) == 1
    assert restored.list()[0]['id'] == identity
    with pytest.raises(ValueError, match='independent'):
        run(store, identity)


def test_before_after_and_reverification_require_matching_evidence(tmp_path, monkeypatch):
    store, identity = setup(tmp_path)
    review(store, identity)
    current = outcome('failed')

    async def runner(*args, **kwargs):
        return current

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    assert run(store, identity)['state'] == 'reproduced'
    current = outcome('passed')
    with pytest.raises(ValueError, match='changed source revision'):
        run(store, identity, 'after')
    (tmp_path / 'app.py').write_text('safe = True')
    assert run(store, identity, 'after')['state'] == 'fixed'
    assert run(store, identity, 'after')['state'] == 'reverified'
    assert not store.list()[0]['evidence_stale']
    (tmp_path / 'app.py').write_text('safe = False')
    assert store.list()[0]['evidence_stale']
    current = outcome('failed')
    assert run(store, identity, 'after')['state'] == 'reopened'


@pytest.mark.parametrize('change', ['test', 'image', 'ids'])
def test_changed_regression_contract_cannot_prove_fix(tmp_path, monkeypatch, change):
    store, identity = setup(tmp_path)
    review(store, identity)
    current = outcome('failed')

    async def runner(*args, **kwargs):
        return current

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    run(store, identity)
    current = outcome('passed')
    (tmp_path / 'app.py').write_text('safe = True')
    if change == 'test':
        (tmp_path / 'tests/test_safe.py').write_text('def test_safe():\n    pass\n')
    elif change == 'image':
        current['image_id'] = 'sha256:' + 'b' * 64
    else:
        current['test_ids'] = ['different-test']
    with pytest.raises(ValueError, match='must match'):
        run(store, identity, 'after')
    assert store.get(identity)['state'] == 'reproduced'


@pytest.mark.parametrize('status', ['error', 'unavailable', 'timeout', 'passed'])
def test_unsuccessful_reproduction_never_promotes(tmp_path, monkeypatch, status):
    store, identity = setup(tmp_path)
    review(store, identity)

    async def runner(*args, **kwargs):
        return outcome(status)

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    assert run(store, identity)['state'] == 'reviewed'
    assert store.get(identity)['before'] is None


def test_stale_review_and_changes_during_execution_rejected(tmp_path, monkeypatch):
    store, identity = setup(tmp_path)
    review(store, identity)
    (tmp_path / 'app.py').write_text('safe = True')
    with pytest.raises(ValueError, match='stale'):
        run(store, identity)
    review(store, identity)

    async def runner(*args, **kwargs):
        (tmp_path / 'app.py').write_text('safe = False')
        return outcome('failed')

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    with pytest.raises(ValueError, match='changed during verification'):
        run(store, identity)
    assert store.get(identity)['before'] is None


def test_failed_reproduction_rerun_clears_old_baseline(tmp_path, monkeypatch):
    store, identity = setup(tmp_path)
    review(store, identity)
    current = outcome('failed')

    async def runner(*args, **kwargs):
        return current

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    assert run(store, identity)['state'] == 'reproduced'
    current = outcome('passed')
    assert run(store, identity)['state'] == 'reviewed'
    assert store.get(identity)['before'] is None


def test_unrelated_failing_test_cannot_reopen_finding(tmp_path, monkeypatch):
    store, identity = setup(tmp_path)
    review(store, identity)
    current = outcome('failed')

    async def runner(*args, **kwargs):
        return current

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    run(store, identity)
    current = {**outcome('failed'), 'test_ids': ['unrelated-test']}
    with pytest.raises(ValueError, match='must match'):
        run(store, identity, 'after')
    assert store.get(identity)['state'] == 'reproduced'


def test_suppression_reopen_schema_and_audit(tmp_path):
    store, identity = setup(tmp_path)
    store.suppress(identity, 'Fixture only; reviewed by maintainer')
    with pytest.raises(ValueError, match='independent'):
        run(store, identity)
    store.reopen(identity)
    assert store.get(identity)['state'] == 'candidate'
    with pytest.raises(ValueError):
        FindingReview.model_validate({'verdict': 'supported', 'state': 'fixed'})
    with store.memory._connection() as db:
        events = [json.loads(row[0])['event'] for row in db.execute('SELECT payload FROM security_events ORDER BY seq')]
    assert events == ['discovered', 'human_suppression', 'human_reopened']


def test_independent_review_and_preparation_have_separate_permissions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.test_coordinator import make_coordinator
    from local_coder.types import AgentRole, TaskStatus, ToolName
    store, identity = setup(tmp_path)
    coordinator = make_coordinator(tmp_path)
    registry = coordinator.tool_registry
    original = {role: set(allowed) for role, allowed in registry._permissions.items()}

    async def get_model(role):
        return object()

    def create_agent(role, *args):
        async def execute(task):
            assert not registry.has_permission(role, ToolName.RUN_TESTS)
            assert not registry.has_permission(role, ToolName.RUN_COMMAND)
            assert not registry.has_permission(role, ToolName.RECORD_FINDING)
            assert not registry.has_permission(role, ToolName.MCP_CALL)
            if role == AgentRole.REVIEWER:
                assert not registry.has_permission(role, ToolName.WRITE_FILE)
                value = FindingReview(verdict='supported', input_trace='input to sink', prerequisites='local input',
                                      impact='bad access', counterevidence='no guard found').model_dump_json()
            else:
                denied = await registry.execute_tool(role, 'write_file', {'path': 'app.py', 'content': 'tampered'})
                assert not denied.success
                value = 'Prepared tests/security_poc/test_safe.py; not executed'
            return SimpleNamespace(status=TaskStatus.COMPLETED, summary=value)
        return SimpleNamespace(execute=execute)

    coordinator.model_manager.get_model = get_model
    monkeypatch.setattr('local_coder.orchestrator.coordinator.create_agent', create_agent)
    assert asyncio.run(coordinator.review_security_finding(identity))['state'] == 'reviewed'
    assert 'not executed' in asyncio.run(coordinator.prepare_security_regression(identity))
    assert registry._permissions == original
    assert (tmp_path / 'app.py').read_text() == 'safe = False'


def test_cli_inventory_list_and_labeled_metrics(tmp_path):
    from click.testing import CliRunner
    from local_coder.cli.main import cli
    store, identity = setup(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ['--project', str(tmp_path), 'security-findings', 'list'])
    assert result.exit_code == 0, result.output
    assert identity in result.output
    inventory = runner.invoke(cli, ['--project', str(tmp_path), 'security-findings', 'threat-model'])
    assert inventory.exit_code == 0, inventory.output
    assert 'coverage_gaps' in inventory.output
    dataset = tmp_path / 'labels.json'
    dataset.write_text(json.dumps({'predicted_ids': ['a', 'b'], 'expected_ids': ['a', 'c']}))
    metrics = runner.invoke(cli, ['--project', str(tmp_path), 'security-findings', 'metrics', str(dataset)])
    assert metrics.exit_code == 0, metrics.output
    assert json.loads(metrics.output)['precision'] == 0.5

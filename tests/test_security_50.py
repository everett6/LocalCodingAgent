"""Focused runtime regressions for local security evidence and analysis."""
import asyncio
import hashlib
import json

import pytest

from local_coder.security.ledger import FindingsLedger, LedgerEntry
from local_coder.security.evidence import EvidenceStore
from local_coder.security_reports import export_sarif, import_sarif, findings_report
from local_coder.tools.security import RunSASTTool


def test_occurrences_and_existing_ids_survive_ledger_reload(tmp_path):
    ledger = FindingsLedger.for_project(tmp_path)
    first, _ = ledger.record(LedgerEntry('Issue', 'HIGH', 'app.py', line=2, rule='R'))
    second, created = ledger.record(LedgerEntry('Issue', 'HIGH', 'app.py', line=8, rule='R'))
    assert created and first.id != second.id
    restarted = FindingsLedger.for_project(tmp_path)
    updated, created = restarted.record(LedgerEntry('Updated', 'HIGH', 'app.py', line=2, rule='R'))
    assert not created and updated.id == first.id
    assert len(restarted.entries) == 2


def test_sync_keeps_imported_false_positive_and_legacy_evidence(tmp_path):
    ledger = FindingsLedger.for_project(tmp_path)
    ledger.record(LedgerEntry('Issue', 'HIGH', 'app.py', line=2, rule='R',
                                       status='false_positive', lesson='Reviewed safe boundary'))
    # Emulate a pre-occurrence evidence row. It must migrate rather than duplicate.
    store = EvidenceStore(tmp_path)
    legacy = hashlib.sha256(b'app.py|R').hexdigest()[:24]
    with store.memory._connection() as db:
        db.execute('INSERT INTO security_evidence VALUES (?,?)', (legacy, json.dumps({
            'id': legacy, 'finding': {}, 'state': 'suppressed', 'suppression': 'human decision',
            'review': None, 'before': None, 'after': None})))
    store.sync()
    assert len(store.list()) == 1
    assert store.list()[0]['id'] == legacy
    assert store.list()[0]['state'] == 'suppressed'
    merged, _ = ledger.record(LedgerEntry('New scan text', 'HIGH', 'app.py', line=2, rule='R'))
    assert merged.status == 'false_positive'
    assert merged.lesson == 'Reviewed safe boundary'
    store.sync()
    assert store.list()[0]['suppression'] == 'human decision'


def test_full_scan_archive_round_trip_precedes_prompt_excerpt(tmp_path):
    from local_coder.security.scan_archive import ScanArchive
    from local_coder.types import ToolResult
    store = ScanArchive(tmp_path)
    result = ToolResult(success=True, output='evidence\n' * 6000,
                        data={'coverage': {'reviewed': ['app.py'], 'failed': [], 'unknown': []}})
    reference = store.save('run_sast', {'target_path': '.'}, result)
    restored = store.read(reference)
    assert restored['result']['output'] == result.output
    assert restored['result']['data'] == result.data
    assert len(reference) < 150
    path = tmp_path / reference
    path.write_text('{}')
    with pytest.raises(ValueError, match='digest'):
        store.read(reference)


def test_sast_tracks_reviewed_failed_unsupported_and_surface(tmp_path):
    (tmp_path / 'app.py').write_text('''import os
@app.get('/items')
@requires_login
def route(request):
    value = request.args['name']
    if request.user:
        return service(value)
def service(value):
    cleaned = str(value)
    return os.system(cleaned)
''')
    (tmp_path / 'broken.py').write_text('def broken(:')
    (tmp_path / 'native.rs').write_text('fn main() {}')
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool='builtin'))
    coverage = result.data['coverage']
    assert 'app.py' in coverage['reviewed']
    assert 'broken.py' in str(coverage['failed'])
    assert 'native.rs' in str(coverage['unknown'])
    analysis = result.data['local_analysis']
    assert analysis['entry_points'][0]['function'] == 'route'
    trace = analysis['authorization_traces'][0]
    assert trace['service'] == 'service' and trace['guards']
    assert trace['boundaries'] and trace['gaps']
    finding = result.data['findings'][0]
    assert finding['semantic_trace']['sources']
    assert finding['semantic_trace']['transforms']
    assert finding['semantic_trace']['sink']['line'] == 10
    assert analysis['vulnerability_classes']
    assert coverage['status'] == 'partial'


def test_empty_report_has_unknown_coverage_and_no_safety_claim():
    text = findings_report([])
    assert 'unknown' in text.lower() and 'does not prove safety' in text.lower()


def test_sarif_roundtrip_keeps_suppression_and_finding_package():
    row = {'title': 'Issue', 'path': 'app.py', 'line': 3, 'rule': 'R',
           'status': 'false_positive', 'suppression': 'Locally reviewed',
           'package': {'revision': 'abc123', 'prerequisites': ['local input'],
                       'evidence': ['scan-reference'], 'reproduction': None,
                       'impact': 'unverified candidate', 'remediation': 'guard input',
                       'verification_disposition': 'not_run'}}
    restored = import_sarif(export_sarif([row]))[0]
    assert restored['status'] == 'false_positive'
    assert restored['suppression'] == 'Locally reviewed'
    assert restored['package'] == row['package']
    assert restored['verification'] == 'unverified'


def test_bounded_fixture_fuzzing_is_repeatable_minimized_and_persisted(tmp_path):
    from local_coder.security.local_fuzz import run_fixture_fuzz
    first = run_fixture_fuzz(tmp_path, seed=17, max_cases=12, max_steps=32)
    second = run_fixture_fuzz(tmp_path, seed=17, max_cases=12, max_steps=32)
    assert first == second
    assert first['seed'] == 17 and first['executed_cases'] <= 12
    assert first['counterexamples'][0]['minimized'] == '0'
    assert first['negative_controls']['status'] == 'passed'
    assert first['target_scope'] == 'fixed_synthetic_fixture'
    assert (tmp_path / first['archive']).is_file()
    with pytest.raises(ValueError):
        run_fixture_fuzz(tmp_path, seed=17, max_cases=1000000)


def test_proof_package_does_not_invent_missing_controls(tmp_path):
    ledger = FindingsLedger.for_project(tmp_path)
    ledger.record(LedgerEntry('Issue', 'HIGH', 'app.py', line=2, rule='R'))
    store = EvidenceStore(tmp_path)
    store.sync()
    identity = store.list()[0]['id']
    package = store.export_package(identity)
    assert package['proof_stage'] == 'scanner_candidate'
    assert package['negative_controls']['status'] == 'missing'
    assert package['verification_disposition'] == 'unverified'
    assert set(('revision', 'prerequisites', 'evidence', 'reproduction', 'impact',
                'remediation', 'verification_disposition')) <= package.keys()


def test_agent_prompt_retains_recoverable_full_scan_reference(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from local_coder.agents.base import BaseAgent
    from local_coder.agents.security import SecurityAgent
    from local_coder.security.scan_archive import ScanArchive
    from local_coder.tools import create_tool_registry
    from local_coder.types import AgentTask, ToolResult
    registry = create_tool_registry(str(tmp_path))
    full = 'original scanner evidence\n' * 2000

    async def execute_tool(*args, **kwargs):
        return ToolResult(success=True, output=full, data={'coverage': {'status': 'unknown'}})

    async def capture(self, task):
        return task

    monkeypatch.setattr(registry, 'execute_tool', execute_tool)
    monkeypatch.setattr(BaseAgent, 'execute', capture)
    agent = SecurityAgent(SimpleNamespace(config=SimpleNamespace(profile='generic')), registry)
    task = asyncio.run(agent.execute(AgentTask(role=agent.role, objective='review local source')))
    for finding in task.context.previous_findings:
        reference = finding.splitlines()[0].removeprefix('Full scan archive: ')
        assert ScanArchive(tmp_path).read(reference)['result']['output'] == full
        assert len(finding) < 16300
        assert len(finding) < len(full)


def test_controlled_proof_requires_observed_controls_and_matching_lineage(tmp_path, monkeypatch):
    from local_coder.security.evidence import FindingReview
    from local_coder.memory.goals import GoalLedger
    ledger = FindingsLedger.for_project(tmp_path)
    ledger.record(LedgerEntry('Issue', 'HIGH', 'app.py', line=2, rule='R'))
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_local.py').write_text('def test_local():\n    assert False\n')
    revision = ['before-source']
    monkeypatch.setattr(GoalLedger, 'revision', lambda self: revision[0])
    store = EvidenceStore(tmp_path)
    store.sync()
    identity = store.list()[0]['id']
    store.review(identity, FindingReview(verdict='supported', input_trace='local input to sink',
                 prerequisites='local fixture', impact='wrong policy decision', counterevidence='guard inspected'),
                 reviewer_task='review-task', revision=revision[0], finding=store.get(identity)['finding'])
    ids = ['tests/test_local.py::test_local', 'tests/test_local.py::test_negative_control']
    outcome = {'status': 'failed', 'failed': 1, 'passed': 1, 'errors': 0,
               'failure_kind': 'assertion', 'backend': 'docker', 'image_id': 'sha256:' + 'a' * 64,
               'test_ids': ids, 'passed_test_ids': []}

    async def runner(*args, **kwargs):
        return dict(outcome)

    monkeypatch.setattr('local_coder.security.regression.run_regression', runner)
    options = {'phase': 'before', 'test_path': 'tests/test_local.py', 'image': 'local-image',
               'negative_control_ids': [ids[1]]}
    with pytest.raises(ValueError, match='controls did not pass'):
        asyncio.run(store.verify_controlled(identity, **options))
    assert store.get(identity)['state'] == 'reviewed'
    outcome['passed_test_ids'] = [ids[1]]
    result = asyncio.run(store.verify_controlled(identity, **options))
    assert result['strict_reproduced'] and not result['strict_verified']
    revision[0] = 'after-source'
    outcome.update(status='passed', passed=2, failed=0, passed_test_ids=ids)
    result = asyncio.run(store.verify_controlled(identity, **{**options, 'phase': 'after'}))
    assert result['strict_verified'] and result['proof_stage'] == 'verified_fix'
    assert store.export_package(identity)['verification_disposition'] == 'verified'
    revision[0] = 'unreviewed-source'
    assert not store.export_package(identity)['strict_verified']
    assert store.list()[0]['verification_disposition'] == 'stale_evidence'
    store.suppress(identity, 'Reviewed false positive')
    assert not store.get(identity)['strict_verified']


def test_missing_controls_are_explicit_in_legacy_proof(tmp_path):
    finding = {'id': 'test', 'finding': {}, 'suppression': '', 'review': {'verdict': 'supported'},
               'before': {'status': 'failed'}, 'after': {'status': 'passed'}}
    disposition = EvidenceStore._proof_disposition(finding)
    assert not disposition['strict_verified'] and not disposition['strict_reproduced']
    assert disposition['verification_disposition'] == 'unverified_missing_controls'


def test_fuzz_fixture_uses_existing_approval_and_is_exposed_in_tool(tmp_path):
    from local_coder.safety import SafetyHarness
    from local_coder.tools.exploit import GenerateFuzzCasesTool
    options = dict(snippet='inert fixture context', synthetic_lab=True, seed=2, run_local_fixture=True)
    denied = asyncio.run(GenerateFuzzCasesTool(str(tmp_path), SafetyHarness(1)).execute(**options))
    assert not denied.success
    assert not (tmp_path / '.local-coder/security/fuzz').exists()
    accepted = asyncio.run(GenerateFuzzCasesTool(str(tmp_path), SafetyHarness(1), lambda _: True).execute(**options))
    assert accepted.success
    assert accepted.data['fixture_evidence']['executed_cases'] <= 12
    assert accepted.data['fixture_evidence']['archive']
    assert accepted.data['verification'] == 'unverified'


def test_security_cli_exports_qualified_package_and_requires_controls(tmp_path):
    from click.testing import CliRunner
    from local_coder.security.cli import security_findings
    ledger = FindingsLedger.for_project(tmp_path)
    ledger.record(LedgerEntry('Issue', 'HIGH', 'app.py', line=2, rule='R'))
    store = EvidenceStore(tmp_path)
    store.sync()
    identity = store.list()[0]['id']
    runner = CliRunner()
    result = runner.invoke(security_findings, ['export', identity], obj={'project_root': str(tmp_path)})
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['negative_controls']['status'] == 'missing'
    result = runner.invoke(security_findings, ['verify', identity, '--phase', 'before',
                           '--test', 'tests/test_local.py', '--image', 'local-image'],
                           obj={'project_root': str(tmp_path)})
    assert result.exit_code != 0 and 'negative-control' in result.output


def test_scanner_fingerprints_separate_identical_occurrences_and_migrate_baselines(tmp_path):
    from local_coder.security.triage import triage, fingerprint
    from local_coder.tools.security import Finding
    source = 'eval(value)\neval(value)\n'
    (tmp_path / 'app.py').write_text(source)
    findings = [Finding('app.py', line, 'HIGH', 'builtin', 'R', 'candidate') for line in [1, 2]]
    initial = triage(findings, tmp_path, [], None)
    assert len(set(initial.fingerprints.values())) == 2
    legacy = fingerprint('builtin', 'R', 'app.py', 'eval(value)', 1)
    assert not triage(findings, tmp_path, [], {legacy}).new
    (tmp_path / 'app.py').write_text('# shift\n' + source)
    moved = [Finding('app.py', line, 'HIGH', 'builtin', 'R', 'candidate') for line in [2, 3]]
    assert set(triage(moved, tmp_path, [], None).fingerprints.values()) == set(initial.fingerprints.values())


def test_full_baseline_security_scan_is_archived_even_when_no_scanner_available(tmp_path, monkeypatch):
    from local_coder.tools.security import SecurityScanTool
    from local_coder.security.scan_archive import ScanArchive
    monkeypatch.setattr('local_coder.tools.security.find_executable', lambda *args: None)
    result = asyncio.run(SecurityScanTool(str(tmp_path)).execute(scanners=['bandit']))
    assert not result.success
    record = ScanArchive(tmp_path).read(result.data['archive_reference'])
    assert record['result']['data']['coverage']['failed']


@pytest.mark.parametrize('engine', ['semgrep', 'bandit'])
@pytest.mark.parametrize('tool_kind', ['sast', 'baseline'])
def test_structured_scanner_capture_preserves_large_original_json(tmp_path, monkeypatch, engine, tool_kind):
    import base64
    from local_coder.security.scan_archive import ScanArchive
    from local_coder.tools.security import SecurityScanTool
    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / '.semgrep.yml').write_text('rules: []\n')
    # This unused scanner metadata is intentionally larger than the ordinary
    # 200 KB log-tail limit and must survive byte-for-byte, before JSON parsing.
    original = json.dumps({'results': [], 'metadata': 'original-head-' + 'x' * 240000 + '-original-tail'}).encode()
    diagnostics = b'complete scanner stderr\n\xff'

    class Process:
        stdin = None
        returncode = 0
        def __init__(self):
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_data(original)
            self.stdout.feed_eof()
            self.stderr.feed_data(diagnostics)
            self.stderr.feed_eof()
        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr('local_coder.tools.security.asyncio.create_subprocess_exec', spawn)
    monkeypatch.setattr('local_coder.tools.security.shutil.which', lambda name: 'local-scanner')
    monkeypatch.setattr('local_coder.tools.security.find_executable', lambda *args: 'local-scanner')
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool=engine) if tool_kind == 'sast' else
                         SecurityScanTool(str(tmp_path)).execute(scanners=[engine]))
    assert result.success, result.error or result.output
    reference = result.data['scanner_capture_archives'][engine]
    captured = ScanArchive(tmp_path).read(reference)['result']['data']
    assert base64.b64decode(captured['stdout_base64']) == original
    assert base64.b64decode(captured['stderr_base64']) == diagnostics
    assert captured['capture']['complete'] is True


@pytest.mark.parametrize('engine', ['semgrep', 'bandit'])
@pytest.mark.parametrize('tool_kind', ['sast', 'baseline'])
def test_structured_scanner_over_limit_is_explicit_failure(tmp_path, monkeypatch, engine, tool_kind):
    from local_coder.security.scan_archive import ScanArchive
    from local_coder.tools.security import SecurityScanTool
    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / '.semgrep.yml').write_text('rules: []\n')
    original = json.dumps({'results': [], 'metadata': 'x' * 2048}).encode()

    class Process:
        stdin = None
        returncode = 0
        def __init__(self):
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_data(original)
            self.stdout.feed_eof()
            self.stderr.feed_eof()
        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr('local_coder.tools.security.asyncio.create_subprocess_exec', spawn)
    monkeypatch.setattr('local_coder.tools.security.shutil.which', lambda name: 'local-scanner')
    monkeypatch.setattr('local_coder.tools.security.find_executable', lambda *args: 'local-scanner')
    # Keep the regression tiny while exercising the actual bounded stream reader.
    monkeypatch.setattr('local_coder.tools.security.MAX_SCANNER_STREAM_BYTES', 1024, raising=False)
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool=engine) if tool_kind == 'sast' else
                         SecurityScanTool(str(tmp_path)).execute(scanners=[engine]))
    assert not result.success
    assert 'capture limit' in ((result.error or '') + result.output).lower()
    assert result.data['coverage']['failed']
    assert result.data['coverage']['status'] != 'complete'
    captured = ScanArchive(tmp_path).read(result.data['scanner_capture_archives'][engine])['result']['data']
    assert captured['capture']['complete'] is False
    assert captured['capture']['disposition'] == 'over_limit'


@pytest.mark.parametrize('raw', [b'{"results": [', b'{}', b'{"results": {}}'])
@pytest.mark.parametrize('tool_kind', ['sast', 'baseline'])
def test_incomplete_scanner_json_is_not_a_negative_result(tmp_path, monkeypatch, raw, tool_kind):
    import base64
    from local_coder.security.scan_archive import ScanArchive
    from local_coder.tools.security import SecurityScanTool
    (tmp_path / 'app.py').write_text('value = 1\n')

    class Process:
        stdin = None
        returncode = 0
        def __init__(self):
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_data(raw)
            self.stdout.feed_eof()
            self.stderr.feed_eof()
        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr('local_coder.tools.security.asyncio.create_subprocess_exec', spawn)
    monkeypatch.setattr('local_coder.tools.security.shutil.which', lambda name: 'local-scanner')
    monkeypatch.setattr('local_coder.tools.security.find_executable', lambda *args: 'local-scanner')
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool='bandit') if tool_kind == 'sast' else
                         SecurityScanTool(str(tmp_path)).execute(scanners=['bandit']))
    assert not result.success
    assert 'invalid structured JSON' in ((result.error or '') + result.output)
    assert result.data['coverage']['failed']
    capture = ScanArchive(tmp_path).read(result.data['scanner_capture_archives']['bandit'])['result']['data']
    assert base64.b64decode(capture['stdout_base64']) == raw
    assert capture['capture']['scope'] == 'raw_process_bytes_only'


def test_baseline_preserves_observed_scanner_errors(tmp_path, monkeypatch):
    from local_coder.tools.security import SecurityScanTool, _ScannerCapture
    payload = {'results': [], 'errors': [{'type': 'ParseError', 'message': 'Source could not be parsed'}]}
    async def capture(*args, **kwargs):
        return _ScannerCapture(1, json.dumps(payload), payload, 'raw-capture-reference')
    monkeypatch.setattr('local_coder.tools.security._capture_scanner', capture)
    monkeypatch.setattr('local_coder.tools.security.find_executable', lambda *args: 'bandit')
    result = asyncio.run(SecurityScanTool(str(tmp_path)).execute(scanners=['bandit']))
    assert not result.success
    assert result.data['coverage']['status'] == 'partial'
    assert result.data['coverage']['failed']
    assert 'scanner errors' in result.output
    assert json.loads(result.data['scanner_outputs']['bandit']) == payload

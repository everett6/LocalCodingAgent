import asyncio
import json
import time

from local_coder.remote import RemoteControlServer
from local_coder.tools import create_tool_registry
from local_coder.types import AgentRole, ToolName


def test_local_audit_exports_traceable_sarif_and_markdown(tmp_path):
    (tmp_path / 'app.py').write_text('import hashlib\ndef digest(value):\n return hashlib.md5(value).hexdigest()\n')
    server = RemoteControlServer(str(tmp_path))
    code, result = server.handle('POST', '/research/audit', {'target_path': '.'})
    assert code == 200 and result['findings']
    assert all(item['verification'] == 'unverified' for item in result['findings'])
    assert len(result['evidence_hash']) == 64
    code, evidence = server.handle('GET', '/research/evidence')
    assert code == 200 and evidence['integrity']['valid']
    assert evidence['integrity']['records'] == 1
    sarif = server.handle('GET', '/research/report?format=sarif')[1]
    assert sarif['version'] == '2.1.0'
    assert sarif['runs'][0]['results'][0]['locations'][0]['physicalLocation']['artifactLocation']['uri'] == 'app.py'
    assert 'Security findings' in server.handle('GET', '/research/report?format=markdown')[1]['markdown']
    before = (tmp_path / 'app.py').read_text()
    assert server.handle('POST', '/research/sarif/import', {'report': sarif})[0] == 200
    assert (tmp_path / 'app.py').read_text() == before


def test_research_endpoints_reject_escape_and_do_not_elevate_imported_verification(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    assert server.handle('POST', '/research/audit', {'target_path': '../'})[0] == 400
    assert server.handle('POST', '/research/audit', {'unexpected': True})[0] == 400
    assert server.handle('GET', '/research/report?format=html')[0] == 400
    report = {'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 'fixture'}}, 'results': [{
        'ruleId': 'fixture', 'message': {'text': 'Reported candidate'},
        'properties': {'verification': 'verified'},
        'locations': [{'physicalLocation': {'artifactLocation': {'uri': '../outside.py'}, 'region': {'startLine': 1}}}],
    }]}]}
    assert server.handle('POST', '/research/sarif/import', {'report': report})[0] == 400
    assert server.handle('GET', '/research/evidence')[1]['integrity']['records'] == 0


def test_structure_route_and_exact_capability_inventory(tmp_path):
    (tmp_path / 'app.py').write_text('def inspect_fixture(value):\n return value\n')
    server = RemoteControlServer(str(tmp_path))
    server.context_memory.index_code()
    code, structure = server.handle('GET', '/memory/structure?path=app.py&query=inspect_fixture&budget=1000')
    assert code == 200 and 'inspect_fixture' in structure['text']
    assert len(structure['text']) <= 1000
    assert server.handle('GET', '/memory/structure?path=../')[0] == 400
    code, catalog = server.handle('GET', '/research/capabilities')
    assert code == 200 and catalog['total'] == 200
    assert sum(catalog['counts'].values()) == 200
    assert catalog['counts']['planned'] > 0


def test_analysis_tool_is_read_only_and_cannot_bypass_engagement_scope(tmp_path):
    (tmp_path / 'app.py').write_text('import hashlib\nhashlib.md5(input())\n')
    registry = create_tool_registry(str(tmp_path))
    assert registry.has_permission(AgentRole.SECURITY, ToolName.ADVANCED_AUDIT)
    assert registry.has_permission(AgentRole.BUG_HUNTER, ToolName.ADVANCED_AUDIT)
    assert not registry.has_permission(AgentRole.RED_TEAM, ToolName.ADVANCED_AUDIT)
    result = asyncio.run(registry.execute_tool(AgentRole.SECURITY, 'advanced_audit', {'target_path': 'app.py'}))
    assert result.success and result.data['findings']


def test_blocked_work_is_visible_as_blocked_without_false_success(tmp_path, monkeypatch):
    from local_coder.orchestrator.blocked import AgentBlocked
    from local_coder.types import AgentResponse, TaskStatus
    server = RemoteControlServer(str(tmp_path))
    class Coordinator:
        async def run(self, request, **options):
            raise AgentBlocked(AgentResponse(task_id='fixture', status=TaskStatus.BLOCKED, summary='Recorded scope blocker. Source baseline remains available.'))
        class Manager:
            async def close_all(self):
                pass
        model_manager = Manager()
    monkeypatch.setattr(server, '_coordinator', lambda: Coordinator())
    code, job = server.handle('POST', '/jobs', {'request': 'Review local code'})
    assert code == 202
    for _ in range(200):
        record = server.sessions.get(job['session_id'])
        if record['status'] not in {'queued', 'running'}:
            break
        time.sleep(.01)
    assert record['status'] == 'blocked'
    assert 'baseline remains' in record['result']
    assert not any(event['event_type'] == 'job_finished' for event in server.events)


def test_exports_keep_source_hashes_and_qualify_changed_sources(tmp_path):
    source = tmp_path / 'app.py'
    source.write_text('import hashlib\nhashlib.md5(input())\n')
    server = RemoteControlServer(str(tmp_path))
    audit = server.handle('POST', '/research/audit', {})[1]
    fingerprint = audit['findings'][0]['evidence']['source_sha256']
    sarif = server.handle('GET', '/research/report')[1]
    properties = sarif['runs'][0]['results'][0]['properties']
    assert properties['source_sha256'] == fingerprint
    assert properties['source_status'] == 'unchanged'
    source.write_text('print("fixed fixture")\n')
    sarif = server.handle('GET', '/research/report')[1]
    assert sarif['runs'][0]['results'][0]['properties']['source_status'] == 'changed'
    assert server.handle('POST', '/research/audit', {})[0] == 200
    assert not server.handle('GET', '/research/report')[1]['runs'][0]['results']
    assert server.handle('GET', '/research/evidence')[1]['integrity']['records'] == 2


def test_exports_never_silently_truncate_stored_findings(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    findings = [{'path': f'file{i}.py', 'line': 1, 'rule': 'fixture', 'title': 'Reported candidate', 'verification': 'unverified'} for i in range(1001)]
    server._research_store().append('sarif_import', {'findings': findings})
    code, result = server.handle('GET', '/research/report')
    assert code == 400 and 'silently omitted' in result['error']
    assert server.handle('GET', '/research/report?path=file0.py')[0] == 200


def test_root_exports_preserve_imported_results_without_physical_locations(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    report = {'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 'fixture'}}, 'results': [
        {'ruleId': 'unlocated', 'message': {'text': 'Unlocated imported candidate'}},
        {'ruleId': 'logical', 'message': {'text': 'Logical imported candidate'},
         'locations': [{'logicalLocations': [{'name': 'application'}]}]},
    ]}]}
    assert server.handle('POST', '/research/sarif/import', {'report': report})[0] == 200
    for route in ('/research/report', '/research/report?path=.'):
        code, exported = server.handle('GET', route)
        assert code == 200
        results = exported['runs'][0]['results']
        assert {item['ruleId'] for item in results} == {'unlocated', 'logical'}
        assert all('locations' not in item for item in results)
        assert all(item['properties']['source_status'] == 'not_recorded' for item in results)
        assert all(item['properties']['verification'] == 'unverified' for item in results)
    markdown = server.handle('GET', '/research/report?format=markdown&path=.')[1]['markdown']
    assert 'Recorded findings: 2.' in markdown
    assert 'Unlocated imported candidate' in markdown


def test_subpath_reports_qualify_omission_of_unlocated_results(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    server._research_store().append('sarif_import', {'findings': [
        {'rule': 'unlocated', 'title': 'Unlocated candidate'},
        {'path': 'src/app.py', 'rule': 'located', 'title': 'Located candidate'},
    ]})
    code, exported = server.handle('GET', '/research/report?path=src')
    assert code == 200
    assert [item['ruleId'] for item in exported['runs'][0]['results']] == ['located']
    scope = exported['runs'][0]['properties']['report_scope']
    assert scope['path'] == 'src'
    assert scope['unlocated_findings'] == 'excluded'
    markdown = server.handle('GET', '/research/report?format=markdown&path=src')[1]['markdown']
    assert 'Findings without recorded file locations are excluded' in markdown
    assert 'Recorded findings: 1.' in markdown


def test_report_freshness_does_not_read_private_or_escaped_sources(tmp_path, monkeypatch):
    from pathlib import Path
    source = tmp_path / 'app.py'
    source.write_text('fixture')
    (tmp_path / '.env').write_text('private fixture')
    (tmp_path / 'outside.py').symlink_to(tmp_path.parent / 'outside.py')
    server = RemoteControlServer(str(tmp_path))
    server._research_store().append('sarif_import', {'findings': [
        {'path': '.env', 'source_sha256': 'a' * 64, 'rule': 'private'},
        {'path': 'outside.py', 'source_sha256': 'b' * 64, 'rule': 'escape'},
        {'path': 'app.py', 'source_sha256': 'c' * 64, 'rule': 'hashed'},
        {'path': 'app.py', 'rule': 'unhashed'},
    ]})
    original_open = Path.open
    def checked_open(path, *args, **kwargs):
        assert path.name not in {'.env', 'outside.py'}, 'Source freshness must not read private or escaped files'
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', checked_open)
    results = server.handle('GET', '/research/report')[1]['runs'][0]['results']
    statuses = {item['ruleId']: item['properties']['source_status'] for item in results}
    assert statuses == {'private': 'unavailable', 'escape': 'unavailable', 'hashed': 'changed', 'unhashed': 'not_recorded'}


def test_dependency_routes_import_operator_evidence_and_query_exact_versions(tmp_path):
    (tmp_path / 'requirements.txt').write_text('fixture==1.0.0\nunresolved>=2\n')
    server = RemoteControlServer(str(tmp_path))
    code, before = server.handle('GET', '/research/dependencies')
    assert code == 200 and before['cache_status'] == 'cache_absent'
    assert before['coverage']['status'] == 'partial'
    assert before['no_match_is_not_safety_evidence'] is True
    dataset = {'version': 1, 'advisories': [{
        'id': 'CVE-2026-12345', 'ecosystem': 'pypi', 'package': 'fixture',
        'versions': ['1.0.0'], 'summary': 'Synthetic imported candidate',
    }]}
    code, result = server.handle('POST', '/research/advisories/import', {'dataset': dataset, 'source_label': 'operator fixture'})
    assert code == 200 and result['imported'] == 1
    assert result['verification'] == 'unverified'
    code, query = server.handle('GET', '/research/advisories?ecosystem=pypi&package=fixture&version=1.0.0')
    assert code == 200 and len(query['matches']) == 1
    assert query['provenance']['source_label'] == 'operator fixture'
    code, audit = server.handle('GET', '/research/dependencies?path=requirements.txt&max_components=10')
    assert code == 200 and len(audit['matches']) == 1
    assert audit['matches'][0]['verification'] == 'unverified'
    code, inventory = server.handle('GET', '/research/dependencies?action=inventory')
    assert code == 200 and len(inventory['components']) == 2
    assert 'matches' not in inventory
    assert server.handle('GET', '/research/advisories?advisory_id=CVE-2026-12345')[1]['matches']


def test_dependency_routes_reject_invalid_arguments_and_imports_during_active_tasks(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    for route in (
        '/research/dependencies?path=../outside',
        '/research/dependencies?action=import',
        '/research/dependencies?max_components=0',
        '/research/dependencies?max_manifests=201',
        '/research/dependencies?path=.&path=other',
        '/research/dependencies?unknown=field',
        '/research/advisories?ecosystem=unsupported-secret-value',
        '/research/advisories?package=fixture',
        '/research/advisories?advisory_id=invalid-secret-value',
        '/research/advisories?version=1&version=2',
        '/research/advisories?unknown=field',
    ):
        code, result = server.handle('GET', route)
        assert code == 400
        assert 'secret-value' not in json.dumps(result)
    dataset = {'version': 1, 'advisories': []}
    for body in ({'report': dataset}, {'dataset': dataset, 'unknown': True}, {'dataset': dataset, 'source_label': False}):
        assert server.handle('POST', '/research/advisories/import', body)[0] == 400
    server.active_job = {'id': 'fixture'}
    assert server.handle('POST', '/research/advisories/import', {'dataset': dataset})[0] == 400
    assert not (tmp_path / '.local-coder/security/advisories.json').exists()

"""Focused UI summaries, observed states, recovery and research evidence regressions."""
import asyncio
import json
import sys
from datetime import UTC, datetime

from local_coder.remote import RemoteControlServer


def test_idle_summary_omits_reports_and_reuses_unchanged_journal(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    server.sessions.save('one', request='Inspect', phase='run', status='completed', result='large report', events=[{'message': 'large event'}])
    status, summary = server.handle('GET', '/status?summary=1')
    assert status == 200
    assert summary['sessions'][0]['observed_state'] == 'completed_unverified'
    assert 'result' not in summary['sessions'][0]
    assert 'events' not in summary['sessions'][0]
    monkeypatch.setattr(server.sessions, 'list', lambda: (_ for _ in ()).throw(AssertionError('idle journal re-read')))
    assert server.handle('GET', '/status?summary=1')[0] == 200
    assert server.handle('GET', '/jobs/one')[1]['result'] == 'large report'


def test_legacy_status_keeps_full_ide_contract(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    server.sessions.save('one', request='Inspect', status='completed', result='Saved report')
    assert server.handle('GET', '/status')[1]['sessions'][0]['result'] == 'Saved report'


def test_observed_states_require_evidence():
    from local_coder.workspace_status import observed_state, recovery_explanation
    now = datetime.now(UTC)
    assert observed_state({'status': 'running'}, live=True, waiting=True, now=now)['observed_state'] == 'waiting'
    assert observed_state({'status': 'running', 'updated_at': '2000-01-01T00:00:00'}, live=True, now=now)['observed_state'] == 'stalled'
    assert observed_state({'status': 'running'}, live=False, now=now)['observed_state'] == 'incomplete'
    assert observed_state({'status': 'blocked'})['observed_state'] == 'blocked'
    assert observed_state({'status': 'completed', 'verification': {'passed': True, 'command': 'pytest', 'recorded_at': now.isoformat()}})['observed_state'] == 'verified'
    assert observed_state({'status': 'completed', 'verification': {'passed': True}})['observed_state'] == 'completed_unverified'
    assert recovery_explanation({'checkpoint': 'saved', 'quota_notice': {'recovery_required': True}})['kind'] == 'reconciliation_required'
    assert recovery_explanation({'checkpoint': 'saved'})['kind'] == 'saved_boundary'
    assert recovery_explanation({})['kind'] == 'new_work'


class SearchManager:
    def __init__(self, root, servers=None, catalog=None):
        self.project_root = root
        self.servers = servers if servers is not None else {'docs': {'enabled': True, 'credentials_available': True, 'read_only_tools': ['web_search']}}
        self.catalog = catalog if catalog is not None else [{'name': 'web_search', 'version': '2.0', 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}]
        self.calls = []

    def status(self):
        return {'servers': self.servers}

    async def list_tools(self, server):
        self.calls.append(('list', server))
        return self.catalog

    async def call_tool(self, server, tool, arguments):
        self.calls.append(('call', server))
        return {'results': [{'url': 'https://example.test/docs', 'title': 'Docs', 'document_version': 'Python 3.13'}]}


def test_search_doctor_stays_local_until_explicit_probe(tmp_path):
    from local_coder.tools.web_search import search_connection_doctor
    manager = SearchManager(tmp_path)
    local = asyncio.run(search_connection_doctor(manager))
    assert local['servers']['docs']['state'] == 'unverified'
    assert manager.calls == []
    checked = asyncio.run(search_connection_doctor(manager, server='docs', probe=True))
    assert checked['servers']['docs']['state'] == 'ready'
    assert checked['servers']['docs']['tool'] == 'web_search'
    assert manager.calls == [('list', 'docs')]


def test_search_doctor_missing_credentials_disabled_and_bad_contract(tmp_path):
    from local_coder.tools.web_search import search_connection_doctor
    manager = SearchManager(tmp_path, servers={'off': {'enabled': False}, 'missing': {'enabled': True, 'credentials_available': False}})
    result = asyncio.run(search_connection_doctor(manager))
    assert result['servers']['off']['state'] == 'disabled'
    assert result['servers']['missing']['state'] == 'missing_credentials'
    assert manager.calls == []
    manager = SearchManager(tmp_path, catalog=[{'name': 'web_search', 'inputSchema': {'properties': {'query': {}}, 'required': ['secret']}}])
    assert asyncio.run(search_connection_doctor(manager, server='docs', probe=True))['servers']['docs']['state'] == 'unsupported'


def test_search_persists_timestamp_citations_provider_tool_and_known_version(tmp_path):
    from local_coder.tools.web_search import WebSearchTool
    from local_coder.security_reports import EvidenceStore
    manager = SearchManager(tmp_path)
    result = asyncio.run(WebSearchTool(manager).execute(query='public reference'))
    assert result.success
    data = json.loads(result.output)
    provenance = data['provenance']
    assert provenance['provider'] == 'docs'
    assert provenance['tool'] == 'web_search'
    assert provenance['tool_version'] == '2.0'
    assert provenance['document_versions'] == ['Python 3.13']
    assert datetime.fromisoformat(provenance['retrieved_at']).tzinfo is not None
    assert provenance['citation_urls'] == ['https://example.test/docs']
    records = EvidenceStore(tmp_path).read()
    assert records[-1]['kind'] == 'research_search'
    assert records[-1]['data']['provenance']['citation_urls'] == provenance['citation_urls']
    assert 'public reference' not in json.dumps(records)


def test_selected_recovery_explains_newer_pending_effects(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    state = tmp_path / '.local-coder' / 'state'
    state.mkdir()
    checkpoint = state / 'checkpoint_run1_2.json'
    checkpoint.write_text('{}')
    pending = state / 'run_run1_pending.json'
    pending.write_text(json.dumps({'iteration': 3, 'tools': [{'mutation_unknown': True}]}))
    server.sessions.save('one', request='Inspect', status='interrupted', checkpoint=str(checkpoint))
    recovery = server.handle('GET', '/jobs/one')[1]['recovery']
    assert recovery['kind'] == 'reconciliation_required'
    assert 'automatic replay is blocked' in recovery['explanation']
    pending.write_text(json.dumps({'iteration': 2}))
    assert server.handle('GET', '/jobs/one')[1]['recovery']['kind'] == 'saved_boundary'


def test_summary_cache_invalidates_on_saved_result_and_never_runs_git(tmp_path, monkeypatch):
    server = RemoteControlServer(str(tmp_path))
    monkeypatch.setattr('local_coder.remote._git', lambda *args: (_ for _ in ()).throw(AssertionError('idle git')))
    server.sessions.save('one', request='Inspect', status='running')
    assert server.handle('GET', '/status?summary=1')[1]['sessions'][0]['observed_state'] == 'incomplete'
    server.sessions.save('one', status='completed', result='Updated report')
    result = server.handle('GET', '/status?summary=1')[1]['sessions'][0]
    assert result['status'] == 'completed'
    assert result['has_result'] is True
    assert 'result' not in result


def test_doctor_host_route_validates_config_without_opening_connections(tmp_path, monkeypatch):
    from local_coder.integrations.mcp import MCPManager
    MCPManager(tmp_path).configure({'servers': {'search': {'transport': 'stdio', 'command': sys.executable, 'args': [], 'enabled': True, 'env': {'SEARCH_TOKEN': 'CLIN_MISSING_TOKEN'}}}})
    monkeypatch.delenv('CLIN_MISSING_TOKEN', raising=False)
    async def forbidden(*args):
        raise AssertionError('automatic MCP connection')
    monkeypatch.setattr(MCPManager, 'list_tools', forbidden)
    server = RemoteControlServer(str(tmp_path))
    code, result = server.handle('GET', '/integrations/search/doctor')
    assert code == 200
    assert result['servers']['search']['state'] == 'missing_credentials'
    code, result = server.handle('POST', '/integrations/search/doctor', {'server': 'search'})
    assert code == 200
    assert result['servers']['search']['state'] == 'missing_credentials'


def test_explicit_doctor_reports_safe_transport_failure(tmp_path):
    from local_coder.integrations.mcp import MCPError
    from local_coder.tools.web_search import search_connection_doctor
    manager = SearchManager(tmp_path)
    async def failed(server):
        raise MCPError('MCP operation timed out; connection closed')
    manager.list_tools = failed
    report = asyncio.run(search_connection_doctor(manager, server='docs', probe=True))
    assert report['servers']['docs']['state'] == 'connection_failed'
    assert 'Review' in report['servers']['docs']['message']


def test_summary_requests_reject_ambiguous_parameters(tmp_path):
    server = RemoteControlServer(str(tmp_path))
    for path in ['/status?summary=0', '/status?summary=1&summary=1', '/status?summary=1&other=1']:
        assert server.handle('GET', path)[0] == 400


def test_background_run_only_reports_verified_after_structured_host_evidence(tmp_path, monkeypatch):
    import threading
    from local_coder.types import AgentEvent
    server = RemoteControlServer(str(tmp_path))
    finished = threading.Event()

    class Coordinator:
        async def run(self, request, **kwargs):
            server._event(AgentEvent(source='ORCHESTRATOR', event_type='verification_result', message='Observed test result', data={'passed': True, 'command': 'pytest tests/test_local.py'}))
            return 'Completed'

        async def close(self):
            finished.set()

    monkeypatch.setattr(server, '_coordinator', Coordinator)
    code, job = server.handle('POST', '/jobs', {'request': 'Inspect'})
    assert code == 202
    assert finished.wait(3)
    with server.lock:
        worker = server._jobs.get(job['session_id'], {}).get('thread')
    if worker:
        worker.join(3)
    detail = server.handle('GET', '/jobs/' + job['session_id'])[1]
    assert detail['observed_state'] == 'verified'
    assert detail['verification']['command'] == 'pytest tests/test_local.py'
    assert server.handle('GET', '/status?summary=1')[1]['sessions'][0]['observed_state'] == 'verified'

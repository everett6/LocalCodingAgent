import asyncio
import json
from pathlib import Path

import pytest

from local_coder.redteam import EngagementStore, EngagementScope
from local_coder.tools.redteam import RedTeamWorkspaceTool, RedTeamHTTPTool, RedTeamFindingTool, RedTeamImportTool


def create(tmp_path, **values):
    (tmp_path / 'app').mkdir(exist_ok=True)
    (tmp_path / 'app/api.py').write_text('eval(user_input)\n')
    return EngagementStore(str(tmp_path)).create(EngagementScope(name='Local exercise', objective='Review trust boundaries', paths=['app'], **values))


def test_scope_is_durable_and_excludes_source(tmp_path):
    record = create(tmp_path, excluded_paths=['app/private'])
    (tmp_path / 'app/private').mkdir()
    (tmp_path / 'app/private/secret.py').write_text('TOP SECRET')
    store = EngagementStore(str(tmp_path))
    tools = RedTeamWorkspaceTool(store, record['id'])
    async def scenario():
        inventory = await tools.execute(action='inventory')
        assert 'app/api.py' in inventory.output and 'secret.py' not in inventory.output
        for path in ['../outside', 'app/private/secret.py', '.local-coder/engagements/' + record['id'] + '.json']:
            denied = await tools.execute(action='read', path=path)
            assert not denied.success
        scan = await tools.execute(action='scan')
        assert scan.data['findings'][0]['verification'] == 'unverified'
        assert scan.data['evidence_id']
    asyncio.run(scenario())
    assert EngagementStore(str(tmp_path)).load(record['id'])['events']


def test_engagement_paths_reject_symlinks_and_invalid_ids(tmp_path):
    record = create(tmp_path)
    (tmp_path / 'app/link').symlink_to(tmp_path.parent)
    with pytest.raises(ValueError): EngagementStore(str(tmp_path)).resolve_source(record, 'app/link/file')
    for value in ['../outside', 'no-such-engagement']:
        with pytest.raises(ValueError): EngagementStore(str(tmp_path)).load(value)


def test_http_is_opt_in_and_scope_cannot_be_expanded_by_tool(tmp_path):
    record = create(tmp_path)
    result = asyncio.run(RedTeamHTTPTool(EngagementStore(str(tmp_path)), record['id']).execute(url='http://127.0.0.1:1/'))
    assert not result.success
    with pytest.raises(ValueError):
        EngagementScope(name='x', objective='x', active_http=True, origins=['https://example.test'])


def test_findings_require_existing_evidence_and_deduplicate(tmp_path):
    record = create(tmp_path)
    store = EngagementStore(str(tmp_path))
    tool = RedTeamFindingTool(store, record['id'])
    async def scenario():
        bad = await tool.execute(title='Possible injection', asset='app/api.py', evidence_ids=['invented'])
        assert not bad.success
        scanned = await RedTeamWorkspaceTool(store, record['id']).execute(action='scan')
        params = dict(title='Possible injection', asset='app/api.py', evidence_ids=[scanned.data['evidence_id']], hypothesis='Check callers and guards', attack_path=['Untrusted input', 'eval sink'], detection_expected='Input validation blocks evaluation')
        first = await tool.execute(**params)
        second = await tool.execute(**params)
        assert first.success and second.success
    asyncio.run(scenario())
    loaded = store.load(record['id'])
    assert len(loaded['findings']) == 1
    assert loaded['findings'][0]['verification'] == 'unverified'
    assert 'not_tested' in json.dumps(loaded['findings'])
    assert 'Unverified' in store.report(record['id'])['markdown']


def test_sarif_import_is_scoped_and_unverified(tmp_path):
    record = create(tmp_path)
    payload = {'version':'2.1.0','runs':[{'results':[{'ruleId':'test','message':{'text':'Candidate'},'locations':[{'physicalLocation':{'artifactLocation':{'uri':'app/api.py'},'region':{'startLine':1}}}]}]}]}
    (tmp_path / 'app/scan.sarif').write_text(json.dumps(payload))
    store=EngagementStore(str(tmp_path))
    imported=asyncio.run(RedTeamImportTool(store,record['id']).execute(path='app/scan.sarif',format='sarif'))
    assert imported.success and imported.data['imported']==1
    assert store.load(record['id'])['findings'][0]['verification']=='unverified'
    payload['runs'][0]['results'][0]['locations'][0]['physicalLocation']['artifactLocation']['uri']='../elsewhere'
    (tmp_path / 'app/scan.sarif').write_text(json.dumps(payload))
    assert not asyncio.run(RedTeamImportTool(store,record['id']).execute(path='app/scan.sarif',format='sarif')).success


def test_real_http_observations_do_not_follow_redirects_or_leak_headers(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(302 if self.path=='/redirect' else 200)
            self.send_header('Location','http://127.0.0.1:1/outside')
            self.send_header('Set-Cookie','secret-session=value')
            self.end_headers()
            self.wfile.write(b'private response body')
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    base=f'http://127.0.0.1:{server.server_port}'
    record=create(tmp_path,origins=[base],active_http=True,authorization='Disposable local lab',max_requests=2,excluded_urls=[base+'/excluded'])
    store=EngagementStore(str(tmp_path));tool=RedTeamHTTPTool(store,record['id'])
    try:
        async def scenario():
            for url in [base+'/excluded',base+'/excluded/child',base+'/safe/%2e%2e/excluded',base+'/?payload=x','http://127.0.0.1:1/']:
                assert not (await tool.execute(url=url)).success
            assert not (await tool.execute(url=base+'/',method='POST')).success
            observed=await tool.execute(url=base+'/redirect')
            assert observed.success and observed.data['redirect_not_followed']
            assert 'private response body' not in observed.output and 'secret-session' not in observed.output
            assert (await tool.execute(url=base+'/')).success
            denied=await RedTeamHTTPTool(EngagementStore(str(tmp_path)),record['id']).execute(url=base+'/')
            assert not denied.success and 'budget' in denied.error.lower()
        asyncio.run(scenario())
        assert calls==['/redirect','/']
        assert store.load(record['id'])['requests_used']==2
    finally:
        server.shutdown();server.server_close();worker.join()


def test_dns_changes_never_retarget_a_saved_engagement(tmp_path,monkeypatch):
    record=create(tmp_path,origins=['http://127.0.0.1:1'],active_http=True,authorization='local lab')
    monkeypatch.setattr('local_coder.tools.redteam.addresses',lambda *args:['127.0.0.2'])
    result=asyncio.run(RedTeamHTTPTool(EngagementStore(str(tmp_path)),record['id']).execute(url='http://127.0.0.1:1/'))
    assert not result.success and 'DNS changed' in result.error
    assert EngagementStore(str(tmp_path)).load(record['id'])['requests_used']==0


def test_engagement_api_defaults_to_local_scope_and_rejects_fix_or_plan(tmp_path):
    from local_coder.remote import RemoteControlServer
    server=RemoteControlServer(str(tmp_path))
    defaults = server.handle('GET', '/engagements/defaults')[1]['settings']
    assert defaults['origins'] == [] and defaults['active_http'] is False
    assert server.handle('POST','/engagements',{'name':'lab','objective':'test','active_http':True,'origins':['https://example.test']})[0]==400
    status,engagement=server.handle('POST','/engagements',{'name':'lab','objective':'test'})
    assert status==201
    for options in [{'fix':True},{'action':'plan'}]:
        assert server.handle('POST','/jobs',{'request':'test','mode':'red_team','engagement_id':engagement['id'],**options})[0]==400
    assert server.handle('GET','/engagements')[1]['engagements'][0]['id']==engagement['id']
    assert server.handle('GET',f"/engagements/{engagement['id']}/report")[0]==200


def test_role_capabilities_never_grant_shell_or_bypass_scope(tmp_path):
    from local_coder.tools import create_tool_registry
    from local_coder.tools.redteam import bind_engagement
    from local_coder.types import AgentRole,ToolName
    record=create(tmp_path)
    registry=create_tool_registry(str(tmp_path))
    bind_engagement(registry,str(tmp_path),record['id'])
    assert not registry.has_permission(AgentRole.RED_TEAM,ToolName.RUN_COMMAND)
    assert not registry.has_permission(AgentRole.RED_TEAM,ToolName.READ_FILE)
    assert not registry.has_permission(AgentRole.RED_TEAM,ToolName.WRITE_FILE)
    assert registry.has_permission(AgentRole.RED_TEAM,ToolName.REDTEAM_WORKSPACE)


def test_evidence_integrity_and_workflow_order_are_checked(tmp_path):
    from local_coder.tools.redteam import RedTeamWorkflowTool
    record=create(tmp_path);store=EngagementStore(str(tmp_path));tool=RedTeamWorkflowTool(store,record['id'])
    assert not asyncio.run(tool.execute(phase='report')).success
    assert asyncio.run(tool.execute(phase='reconnaissance',steps=[{'title':'Map boundaries','status':'in_progress'}])).success
    file=store._path(record['id'])
    payload=json.loads(file.read_text());payload['events'][0]['data']['phase']='report';file.write_text(json.dumps(payload))
    with pytest.raises(ValueError,match='integrity'):store.load(record['id'])


def test_redteam_source_mentions_cannot_bypass_engagement_scope(tmp_path):
    from local_coder.request_context import prepare_request
    from local_coder.types import AgentMode
    (tmp_path/'outside.py').write_text('do_not_attach_this_secret = 1')
    request,mode,action=prepare_request('/redteam inspect @outside.py',AgentMode.CODING,'run',str(tmp_path))
    assert mode==AgentMode.RED_TEAM and action=='run'
    assert 'do_not_attach_this_secret' not in request


def test_redteam_real_model_tool_loop_resume_retains_scope_and_journal(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from local_coder.orchestrator.coordinator import Coordinator
    from local_coder.types import AgentMode,ModelResponse,ProjectConfig,TaskStatus,ToolCall,ToolName,AgentRole
    class Crash(BaseException): pass
    record=create(tmp_path,excluded_paths=['app/private'])
    class Model:
        config=SimpleNamespace(temperature=0.2,max_tokens=100,profile='generic')
        def __init__(self,resuming=False): self.calls=0;self.resuming=resuming
        async def generate(self,messages,**kwargs):
            self.calls+=1
            schemas=str(kwargs)
            assert 'run_command' not in schemas and 'write_file' not in schemas
            if self.resuming:
                assert 'SEC-PY-CODE-EXEC' in str(messages)
                return ModelResponse(content='Unverified eval sink; reproduction not executed.')
            if self.calls==1:return ModelResponse(tool_calls=[ToolCall(name='redteam_workspace',arguments={'action':'scan'})])
            raise Crash()
    async def scenario():
        first=Coordinator(ProjectConfig(mode=AgentMode.RED_TEAM),str(tmp_path))
        # Return the same scripted model across the run.
        model=Model()
        async def get_first(*args):return model
        monkeypatch.setattr(first.model_manager,'get_model',get_first)
        with pytest.raises(Crash):await first.run('Review trust boundaries',engagement_id=record['id'])
        latest=max(first.runtime.state_dir.glob('checkpoint_*.json'),key=lambda file:json.loads(file.read_text())['iteration'])
        assert json.loads(latest.read_text())['task']['engagement_id']==record['id']
        events=len(EngagementStore(str(tmp_path)).load(record['id'])['events'])
        restarted=Coordinator(ProjectConfig(mode=AgentMode.RED_TEAM),str(tmp_path))
        async def get_resumed(*args):return Model(resuming=True)
        monkeypatch.setattr(restarted.model_manager,'get_model',get_resumed)
        result=await restarted.resume_checkpoint(str(latest))
        assert result.status==TaskStatus.COMPLETED
        assert not restarted.tool_registry.has_permission(AgentRole.RED_TEAM,ToolName.RUN_COMMAND)
        assert len(EngagementStore(str(tmp_path)).load(record['id'])['events'])==events
    asyncio.run(scenario())


def test_redteam_job_runs_the_bound_engagement_and_persists_identity(tmp_path,monkeypatch):
    import time
    from local_coder.remote import RemoteControlServer
    from local_coder.models.manager import ModelManager
    from local_coder.types import ModelResponse,ToolCall
    from types import SimpleNamespace
    record=create(tmp_path)
    class Model:
        config=SimpleNamespace(temperature=0.2,max_tokens=100,profile='generic')
        def __init__(self):self.calls=0
        async def generate(self,messages,**kwargs):
            self.calls+=1
            if self.calls==1:return ModelResponse(tool_calls=[ToolCall(name='redteam_workspace',arguments={'action':'scan'})])
            if self.calls==2:
                evidence=json.loads(messages[-1].content)['evidence_id']
                return ModelResponse(tool_calls=[ToolCall(name='redteam_record_finding',arguments={'title':'Possible eval sink','asset':'app/api.py','evidence_ids':[evidence],'hypothesis':'Inspect callers; unverified'})])
            assert json.loads(messages[-1].content)['verification']=='unverified'
            return ModelResponse(content='Static candidate, unverified.')
        async def close(self):pass
    monkeypatch.setattr(ModelManager,'_create_backend',lambda *args:Model())
    server=RemoteControlServer(str(tmp_path))
    server.handle('POST','/settings',{'model_id':'test','backend':'openai_compatible','base_url':'http://localhost:1234/v1'})
    status,job=server.handle('POST','/jobs',{'request':'Review boundaries','mode':'red_team','engagement_id':record['id']})
    assert status==202
    for _ in range(300):
        saved=server.sessions.get(job['session_id'])
        if saved['status'] not in {'queued','running'}:break
        time.sleep(.01)
    assert saved['status']=='completed',saved
    assert saved['engagement_id']==record['id']
    assert 'Evidence journal' in saved['result']
    assert len(EngagementStore(str(tmp_path)).load(record['id'])['findings'])==1
    assert EngagementStore(str(tmp_path)).load(record['id'])['events']


def test_windows_locking_uses_a_writable_text_lockfile(tmp_path,monkeypatch):
    import sys
    from types import SimpleNamespace
    import local_coder.file_lock as locks
    calls=[]
    monkeypatch.setitem(sys.modules,'msvcrt',SimpleNamespace(LK_LOCK=1,LK_NBLCK=2,LK_UNLCK=3,locking=lambda *args:calls.append(args)))
    monkeypatch.setattr(locks,'os',SimpleNamespace(name='nt'))
    record=create(tmp_path)
    EngagementStore(str(tmp_path)).evidence(record['id'],'inventory',{'files':[]})
    assert len(calls)==2


def test_model_cannot_claim_verified_exploitation_or_detection(tmp_path):
    record=create(tmp_path);store=EngagementStore(str(tmp_path))
    evidence=store.evidence(record['id'],'source_read',{'path':'app/api.py'})
    tool=RedTeamFindingTool(store,record['id'])
    for extra in [{'verification':'reproduced'},{'detection_outcome':'observed'},{'evidence_ids':['missing']}]:
        result=asyncio.run(tool.execute(title='Candidate',asset='app/api.py',**{'evidence_ids':[evidence],**extra}))
        assert not result.success
    assert not store.load(record['id'])['findings']


def test_import_malformed_reports_and_foreign_evidence_do_not_write_findings(tmp_path):
    record=create(tmp_path);store=EngagementStore(str(tmp_path))
    another=create(tmp_path)
    foreign=store.evidence(another['id'],'source_read',{'path':'app/api.py'})
    assert not asyncio.run(RedTeamFindingTool(store,record['id']).execute(title='x',asset='app/api.py',evidence_ids=[foreign])).success
    for content in ['[]','{"version":"2.1.0","runs":[null]}','not-json']:
        (tmp_path/'app/bad.sarif').write_text(content)
        result=asyncio.run(RedTeamImportTool(store,record['id']).execute(path='app/bad.sarif',format='sarif'))
        assert not result.success
    assert not store.load(record['id'])['events']


def test_http_uses_pinned_ip_original_host_and_tls_sni(tmp_path,monkeypatch):
    import httpx
    seen=[]
    monkeypatch.setattr('local_coder.redteam.addresses',lambda *args:['127.0.0.1'])
    monkeypatch.setattr('local_coder.tools.redteam.addresses',lambda *args:['127.0.0.1'])
    record=create(tmp_path,origins=['https://app.example.test'],active_http=True,authorization='Synthetic transport test')
    original=httpx.AsyncClient
    def respond(request):
        seen.append(request)
        return httpx.Response(200,stream=httpx.ByteStream(b'test'))
    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        assert kwargs.get('verify',True) is True
        return original(**kwargs,transport=httpx.MockTransport(respond))
    monkeypatch.setattr('local_coder.tools.redteam.httpx.AsyncClient',client)
    result=asyncio.run(RedTeamHTTPTool(EngagementStore(str(tmp_path)),record['id']).execute(url='https://app.example.test/'))
    assert result.success,result.error
    assert seen[0].url.host=='127.0.0.1'
    assert seen[0].headers['host']=='app.example.test:443'
    assert seen[0].extensions['sni_hostname']=='app.example.test'


def test_http_cancellation_is_journaled_and_attempt_not_refunded(tmp_path,monkeypatch):
    import httpx
    entered=asyncio.Event()
    record=create(tmp_path,origins=['http://127.0.0.1:1'],active_http=True,authorization='Synthetic test')
    original=httpx.AsyncClient
    async def respond(request):
        entered.set()
        await asyncio.sleep(30)
    monkeypatch.setattr('local_coder.tools.redteam.httpx.AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(respond)))
    async def scenario():
        task=asyncio.create_task(RedTeamHTTPTool(EngagementStore(str(tmp_path)),record['id']).execute(url='http://127.0.0.1:1/'))
        await asyncio.wait_for(entered.wait(),timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())
    saved=EngagementStore(str(tmp_path)).load(record['id'])
    assert saved['requests_used']==1 and saved['events'][-1]['kind']=='http_interrupted'


def test_cli_creates_a_local_engagement_without_live_provider_calls(tmp_path,monkeypatch):
    from click.testing import CliRunner
    from importlib import import_module
    cli=import_module('local_coder.cli.main')
    seen=[]
    class Coordinator:
        class Manager:
            async def close_all(self):pass
        model_manager=Manager()
        async def run(self,request,**kwargs):
            seen.append(kwargs['engagement_id'])
            return 'Unverified local exercise report'
    monkeypatch.setattr(cli,'_build_coordinator',lambda *args:Coordinator())
    result=CliRunner().invoke(cli.cli,['--project',str(tmp_path),'redteam','--target','.','Review trust boundaries'])
    assert result.exit_code==0,result.output
    assert EngagementStore(str(tmp_path)).load(seen[0])['scope']['active_http'] is False
    assert 'Unverified' in result.output


def test_nuclei_import_deduplicates_and_rejects_mixed_out_of_scope_reports(tmp_path):
    record=create(tmp_path,origins=['https://app.example.test'])
    store=EngagementStore(str(tmp_path));tool=RedTeamImportTool(store,record['id'])
    candidate={'template-id':'header-check','matched-at':'https://app.example.test/','info':{'name':'Header candidate','description':'Requires validation','severity':'high'}}
    path=tmp_path/'app/nuclei.jsonl'
    path.write_text('\n'.join(json.dumps(candidate) for _ in range(2)))
    result=asyncio.run(tool.execute(path='app/nuclei.jsonl',format='nuclei_jsonl'))
    assert result.success and result.data['imported']==2
    saved=store.load(record['id'])
    assert len(saved['findings'])==1 and saved['findings'][0]['verification']=='unverified'
    previous=len(saved['events'])
    candidate['matched-at']='https://outside.example.test/'
    path.write_text(json.dumps(candidate))
    assert not asyncio.run(tool.execute(path='app/nuclei.jsonl',format='nuclei_jsonl')).success
    assert len(store.load(record['id'])['events'])==previous

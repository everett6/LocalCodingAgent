import subprocess
from types import SimpleNamespace
import pytest
from local_coder.memory.goals import GoalLedger
from local_coder.types import TaskStatus

def repo(tmp_path):
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Test','-c','user.email=test@example.test','commit','--allow-empty','-m','init'],check=True,capture_output=True)
    return GoalLedger(tmp_path)

def test_persistent_goal_evidence_and_workspace_invalidation(tmp_path):
    ledger=repo(tmp_path)
    parent=ledger.create('Fix parser',criteria='Parser regressions pass')
    child=ledger.create('Test parser',parent['id'])
    with pytest.raises(ValueError,match='Completion'):
        ledger.update(child['id'],{'status':'completed'})
    ledger.update(child['id'],{'status':'running','decision':'Keep public API'})
    ledger.finish(SimpleNamespace(task_id=child['id'],status=TaskStatus.COMPLETED,tests_run=['pytest parser'],tests_passed=True,summary='Verified'))
    ledger.update(parent['id'],{'status':'completed'})
    restored=GoalLedger(tmp_path)
    assert restored.get(child['id'])['decisions']==['Keep public API']
    assert restored.briefing()==''
    (tmp_path/'parser.py').write_text('changed = True')
    assert restored.get(child['id'])['evidence_stale']
    assert 'stale' in restored.briefing()
    with pytest.raises(ValueError): restored.update(parent['id'],{'status':'completed'})

def test_dependency_gate_and_atomic_events(tmp_path):
    ledger=repo(tmp_path)
    one=ledger.create('First')
    two=ledger.create('Second',dependencies=[one['id']])
    with pytest.raises(ValueError):ledger.update(two['id'],{'status':'running'})
    assert ledger.get(two['id'])['status']=='pending'
    with ledger.memory._connection() as db:
        assert db.execute('SELECT count(*) FROM goal_events').fetchone()[0]==2
    with pytest.raises(ValueError):ledger.update(two['id'],{'evidence':'forged'})

def test_goal_http_routes_and_profile_validation(tmp_path):
    from local_coder.remote import RemoteControlServer
    server=RemoteControlServer(str(tmp_path))
    status,goal=server.handle('POST','/goals',{'objective':'Find local parser bugs'})
    assert status==201
    assert server.handle('GET','/goals')[1]['goals'][0]['id']==goal['id']
    assert server.handle('POST','/goals/update',{'id':goal['id'],'changes':{'status':'completed'}})[0]==400
    assert server.handle('POST','/bug-hunting/models',{'assignments':{'lead':'missing'}})[0]==400
    assert server.handle('POST','/bug-hunting/models',{'assignments':{'lead':''}})[0]==200

def test_bug_models_reach_coordinator_and_survive_restart(tmp_path, monkeypatch):
    from local_coder.remote import RemoteControlServer
    from local_coder.types import AgentMode
    server=RemoteControlServer(str(tmp_path))
    lead=server.provider_profiles.save('GLM 5.3',{'backend':'openai_compatible','model_id':'glm-5.3','base_url':'https://example.test/v1'})
    worker=server.provider_profiles.save('DeepSeek 4.1',{'backend':'openai_compatible','model_id':'chosen-deepseek-id','base_url':'https://example.test/v1'})
    mapping={'lead':lead['id'],'delegate':worker['id'],'exploit':worker['id']}
    assert server.handle('POST','/bug-hunting/models',{'assignments':mapping})[0]==200
    captured={}
    def fake(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(tool_registry=SimpleNamespace(), on_event=lambda callback: None)
    monkeypatch.setattr('local_coder.remote.Coordinator',fake)
    restored=RemoteControlServer(str(tmp_path))
    restored._context.mode=AgentMode.BUG_HUNTING
    restored._coordinator()
    cfg=captured['config']
    assert cfg.models[cfg.agentic.role_models['planner']].model_id=='glm-5.3'
    for role in ['explorer','bug_hunter','exploit_validator']:
        assert cfg.models[cfg.agentic.role_models[role]].model_id=='chosen-deepseek-id'

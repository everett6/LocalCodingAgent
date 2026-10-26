import asyncio
import time
from types import SimpleNamespace

from local_coder.models.manager import ModelManager
from local_coder.remote import RemoteControlServer
from local_coder.types import ModelResponse, ToolCall


def test_real_security_loop_runs_scans_reads_file_and_persists_report(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('def parse(value):\n    return eval(value)\n')
    class Model:
        config = SimpleNamespace(temperature=0.2, max_tokens=500, profile='generic')
        def __init__(self):
            self.calls = 0
        async def generate(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert 'SEC-PY-CODE-EXEC' in str(messages)
                return ModelResponse(tool_calls=[ToolCall(id='inspect', name='read_file', arguments={'path':'app.py'})])
            assert messages[-1].role == 'tool'
            assert 'eval(value)' in messages[-1].content
            return ModelResponse(content='app.py:2 — dynamic execution candidate. Unverified: no reproduction test executed.')
        async def close(self):
            pass
    monkeypatch.setattr(ModelManager, '_create_backend', lambda self, config: Model())
    server = RemoteControlServer(str(tmp_path))
    assert server.handle('POST', '/settings', {'backend':'openai_compatible','model_id':'fake','base_url':'http://localhost:1234/v1'})[0] == 200
    status, job = server.handle('POST', '/jobs', {'request':'Audit @app.py','mode':'cybersecurity','target_path':'app.py'})
    assert status == 202
    for _ in range(300):
        record = server.sessions.get(job['session_id'])
        if record['status'] not in {'queued', 'running'}:
            break
        time.sleep(.01)
    assert record['status'] == 'completed', record
    assert 'Unverified' in record['result']
    assert any(event['event_type'] == 'scan_completed' for event in record['events'])
    assert (tmp_path/'app.py').read_text() == 'def parse(value):\n    return eval(value)\n'
    restarted = RemoteControlServer(str(tmp_path))
    restarted.reconcile_sessions()
    saved = restarted.handle('GET', f"/events?session_id={job['session_id']}")[1]['events']
    assert any(event['event_type'] == 'scan_completed' for event in saved)

import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from local_coder.core.budgets import BudgetLedger, BudgetExceeded, BudgetedModel
from local_coder.core.snapshots import FileSnapshots
from local_coder.core.approvals import ApprovalInbox
from local_coder.types import Message, ModelResponse


def test_budget_reserves_concurrent_calls_and_never_sends_exhausted_request():
    ledger=BudgetLedger(max_requests=1,max_tokens=10000,max_request_tokens=9000)
    model=SimpleNamespace(config=SimpleNamespace(max_tokens=100,model_id='test'))
    calls=[]
    async def generate(messages,**kwargs):
        calls.append(kwargs)
        return ModelResponse(content='ok',prompt_tokens=20,completion_tokens=2)
    model.generate=generate
    async def scenario():
        wrapped=BudgetedModel(model,ledger)
        await wrapped.generate([Message(role='user',content='Inspect')])
        with pytest.raises(BudgetExceeded):await wrapped.generate([Message(role='user',content='Again')])
    asyncio.run(scenario())
    assert len(calls)==1 and ledger.status()['requests']==1
    assert ledger.status()['tokens']==22


def test_dollar_cap_requires_known_prices_before_a_request():
    ledger=BudgetLedger(max_cost_usd=1)
    with pytest.raises(BudgetExceeded,match='pricing'):
        ledger.reserve(SimpleNamespace(max_tokens=100,model_id='test'),[Message(role='user',content='x')],100)
    assert ledger.status()['requests']==0


def test_snapshot_restores_only_unchanged_agent_result_and_preserves_other_files(tmp_path):
    file=tmp_path/'app.py';file.write_text('value = 1\n');other=tmp_path/'other.py';other.write_text('user work')
    store=FileSnapshots(str(tmp_path));record=store.capture(['app.py'])
    file.write_text('value = 2\n');store.complete(record['id'])
    report=store.diff(record['id'])
    assert 'value = 1' in report['diff'] and 'value = 2' in report['diff']
    file.write_text('external edit\n')
    with pytest.raises(ValueError,match='changed since'):store.restore(record['id'])
    assert file.read_text()=='external edit\n'
    file.write_text('value = 2\n');store.restore(record['id'])
    assert file.read_text()=='value = 1\n' and other.read_text()=='user work'
    with pytest.raises(ValueError):store.capture(['../escape'])


def test_new_file_snapshot_rollback_does_not_touch_preexisting_files(tmp_path):
    store=FileSnapshots(str(tmp_path));record=store.capture(['new.py'])
    (tmp_path/'new.py').write_text('new');store.complete(record['id']);store.restore(record['id'])
    assert not (tmp_path/'new.py').exists()


def test_approval_inbox_requires_trusted_response_and_cancels():
    async def scenario():
        inbox=ApprovalInbox(timeout=1)
        waiting=asyncio.create_task(inbox.request('Generate local SQL lab PoC'))
        await asyncio.sleep(.01)
        record=inbox.list()[0]
        assert record['status']=='pending'
        inbox.respond(record['id'],True)
        assert await waiting is True
        waiting=asyncio.create_task(inbox.request('Another request'))
        await asyncio.sleep(.01);waiting.cancel()
        with pytest.raises(asyncio.CancelledError):await waiting
        assert inbox.list()[-1]['status']=='cancelled'
    asyncio.run(scenario())


def test_sandbox_has_no_host_execution_fallback(monkeypatch):
    from local_coder.core.sandbox import SandboxRunner, SandboxUnavailable
    runner=SandboxRunner();runner._status={'ready':False,'reason':'No sandbox'}
    with pytest.raises(SandboxUnavailable):asyncio.run(runner.run('print(1)',{}))


def test_sandbox_rejects_oversized_inputs_before_starting():
    from local_coder.core.sandbox import SandboxRunner
    with pytest.raises(ValueError):asyncio.run(SandboxRunner().run('print(1)',{'x':'a'*70000}))


def test_small_context_model_receives_bounded_tool_catalog(tmp_path):
    from local_coder.agents.base import BaseAgent
    from local_coder.tools import create_tool_registry
    from local_coder.types import AgentRole, AgentTask, TaskStatus
    class TinyAgent(BaseAgent):
        role = AgentRole.CODER
        system_prompt = 'Inspect and verify the local fixture.'
    seen = []
    class Model:
        config = SimpleNamespace(model_id='tiny-local', profile='generic', temperature=0.0, max_tokens=512, context_length=8192)
        async def generate(self, messages, **kwargs):
            import json
            seen.append(kwargs['tools'])
            assert len(json.dumps(kwargs['tools'])) <= 8192
            assert sum(len(message.content) for message in messages) + len(json.dumps(kwargs['tools'])) + 1536 <= 24576
            return ModelResponse(content='Fixture inspected; no changes required.')
    async def scenario():
        registry = create_tool_registry(str(tmp_path))
        try:
            result = await TinyAgent(Model(), registry).execute(AgentTask(role=AgentRole.CODER, objective='Inspect fixture'))
            assert result.status == TaskStatus.COMPLETED
            assert any(schema['function']['name'] == 'read_file' for schema in seen[0])
        finally:
            await registry.close()
    asyncio.run(scenario())


def test_approval_rejects_unreviewable_description_and_expired_response():
    import time
    async def scenario():
        inbox = ApprovalInbox(timeout=.05)
        assert not await inbox.request('a' * 200001)
        assert not inbox.list()
        waiting = asyncio.create_task(inbox.request('Bounded local approval'))
        await asyncio.sleep(.01)
        record = inbox.list()[0]
        with inbox._lock:
            inbox._items[record['id']]['created_at'] = time.time() - 1
        with pytest.raises(ValueError, match='expired'):
            inbox.respond(record['id'], True)
        assert await waiting is False
    asyncio.run(scenario())

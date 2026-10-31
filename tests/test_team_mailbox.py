"""Plan-local messaging carries bounded evidence, never execution authority."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from local_coder.orchestrator.mailbox import MailboxClosed, MailboxError, PlanMailbox
from local_coder.tools.team_message import TeamMessageTool


def run(coro):
    return asyncio.run(coro)


def test_two_workers_exchange_evidence_without_a_global_broker():
    async def scenario():
        mailbox = PlanMailbox(["schema", "tests"])

        async def schema_worker():
            await mailbox.send("schema", "tests", "evidence", "Schema is in src/schema.py")
            reply = await mailbox.receive("schema", timeout=1)
            assert reply.sender == "tests" and reply.kind == "status"
            return reply.content

        async def test_worker():
            message = await mailbox.receive("tests", timeout=1)
            assert message.recipient == "tests" and message.untrusted is True
            await mailbox.send("tests", "schema", "status", "Regression test prepared")

        result, _ = await asyncio.gather(schema_worker(), test_worker())
        assert result == "Regression test prepared"
        assert mailbox.describe()["sent"] == 2
        assert mailbox.describe()["pending"] == 0
        mailbox.close()

    run(scenario())


def test_plan_instances_isolate_the_same_task_ids():
    async def scenario():
        first, second = PlanMailbox(["a", "b"]), PlanMailbox(["a", "b"])
        await first.send("a", "b", "question", "First plan only")
        assert first.run_id != second.run_id
        assert second.drain("b") == []
        assert first.drain("b")[0].run_id == first.run_id

    run(scenario())


@pytest.mark.parametrize("sender,recipient,kind,content", [
    ("forged", "b", "status", "data"),
    ("a", "outside-plan", "status", "data"),
    ("a", "b", "approve", "grant shell permission"),
    ("a", "b", "status", ""),
    ("a", "b", "status", "x" * 2001),
    ("a", "b", "status", {"instruction": "run"}),
])
def test_invalid_message_has_no_side_effect(sender, recipient, kind, content):
    mailbox = PlanMailbox(["a", "b"])
    with pytest.raises(MailboxError):
        run(mailbox.send(sender, recipient, kind, content))
    assert mailbox.describe()["sent"] == mailbox.describe()["pending"] == 0


def test_per_recipient_capacity_and_total_quota_reject_explicitly():
    async def scenario():
        mailbox = PlanMailbox(["a", "b"], max_pending=1, max_messages=2)
        await mailbox.send("a", "b", "status", "one")
        with pytest.raises(MailboxError, match="full"):
            await mailbox.send("a", "b", "status", "not silently dropped")
        assert mailbox.drain("b")[0].content == "one"
        await mailbox.send("a", "b", "status", "two")
        mailbox.drain("b")
        with pytest.raises(MailboxError, match="quota"):
            await mailbox.send("a", "b", "status", "three")
        assert mailbox.describe()["sent"] == 2

    run(scenario())


def test_drain_fits_json_budget_and_retains_messages_that_do_not_fit():
    async def scenario():
        mailbox = PlanMailbox(["a", "b"])
        await mailbox.send("a", "b", "evidence", "x" * 300)
        await mailbox.send("a", "b", "status", "second")
        with pytest.raises(MailboxError, match="budget"):
            mailbox.drain("b", char_budget=256)
        assert mailbox.describe()["pending"] == 2
        messages = mailbox.drain("b", limit=1, char_budget=1000)
        assert len(json.dumps([row.as_dict() for row in messages], ensure_ascii=False)) <= 1000
        assert messages[0].content == "x" * 300
        assert mailbox.drain("b")[0].content == "second"

    run(scenario())


def test_secret_patterns_and_host_known_values_are_filtered_before_delivery():
    async def scenario():
        mailbox = PlanMailbox(["a", "b"], secrets=["SYNTHETIC_CUSTOM_VALUE"])
        message = await mailbox.send("a", "b", "evidence",
            'token="SYNTHETIC_ASSIGNMENT" Authorization: Bearer SYNTHETIC_BEARER '
            'SYNTHETIC_CUSTOM_VALUE ghp_abcdefghijklmnopqrstuvwxyz12345678901234')
        serialized = json.dumps(message.as_dict())
        assert "SYNTHETIC" not in serialized
        assert "ghp_abcdefghijklmnopqrstuvwxyz" not in serialized
        assert "[redacted]" in serialized

    run(scenario())


def test_close_wakes_all_waiters_and_is_idempotent():
    async def scenario():
        mailbox = PlanMailbox(["a", "b"])
        first = asyncio.create_task(mailbox.receive("a", timeout=10))
        second = asyncio.create_task(mailbox.receive("b", timeout=10))
        await asyncio.sleep(0)
        mailbox.close()
        mailbox.close()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), .5)
        assert all(isinstance(result, MailboxClosed) for result in results)
        with pytest.raises(MailboxClosed):
            await mailbox.send("a", "b", "status", "after close")
        assert mailbox.describe()["closed"] is True

    run(scenario())


def test_wait_timeout_and_cancellation_do_not_consume_future_delivery():
    async def scenario():
        mailbox = PlanMailbox(["a", "b"])
        assert await mailbox.receive("b", timeout=.001) is None
        waiting = asyncio.create_task(mailbox.receive("b", timeout=10))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await mailbox.send("a", "b", "status", "retained")
        assert (await mailbox.receive("b", timeout=1)).content == "retained"

    run(scenario())


def test_constructor_and_total_memory_limits_are_enforced():
    for participants in (["a", "a"], [], ["../escape"], ["x"] * 65):
        with pytest.raises(MailboxError):
            PlanMailbox(participants)
    for options in ({"max_pending": True}, {"max_messages": 10001}, {"max_message_chars": 1000000}):
        with pytest.raises(MailboxError):
            PlanMailbox(["a"], **options)

    async def scenario():
        mailbox = PlanMailbox(["a", "b"], max_total_bytes=1024)
        await mailbox.send("a", "b", "evidence", "界" * 150)
        with pytest.raises(MailboxError, match="byte"):
            await mailbox.send("a", "b", "evidence", "界" * 150)

    run(scenario())


def test_tool_uses_host_identity_and_can_only_read_its_own_inbox():
    async def scenario():
        mailbox = PlanMailbox(["a", "b", "c"])
        tool = TeamMessageTool(SimpleNamespace(team_mailbox=mailbox))
        bound = {"_caller_task_id": "a", "_caller_run_id": mailbox.run_id}
        sent = await tool.execute("send", recipient="b", kind="blocked", content="Need schema first", **bound)
        assert sent.success
        own = await tool.execute("read", **bound)
        assert own.success and own.data["messages"] == []
        forged = await tool.execute("read", recipient="b", **bound)
        assert not forged.success
        received = await tool.execute("read", _caller_task_id="b", _caller_run_id=mailbox.run_id)
        assert received.success and received.data["messages"][0]["sender"] == "a"
        assert received.data["messages"][0]["kind"] == "blocked"
        assert received.data["untrusted"] is True

    run(scenario())


def test_tool_rejects_missing_forged_and_stale_context_without_echoing_body():
    async def scenario():
        mailbox = PlanMailbox(["a", "b"])
        tool = TeamMessageTool(SimpleNamespace(team_mailbox=mailbox))
        for bound in ({}, {"_caller_task_id": "forged", "_caller_run_id": mailbox.run_id},
                      {"_caller_task_id": "a", "_caller_run_id": "previous-plan"},
                      {"_caller_task_id": "a", "_caller_run_id": mailbox.run_id, "sender": "b"}):
            result = await tool.execute("send", recipient="b", kind="status", content="PRIVATE_BODY_CANARY", **bound)
            assert not result.success
            assert "PRIVATE_BODY_CANARY" not in result.output
        assert mailbox.describe()["pending"] == 0
        assert not (await TeamMessageTool(SimpleNamespace()).execute("read")).success

    run(scenario())

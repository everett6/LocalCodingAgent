"""Summary-notice efficiency and compaction fidelity survive branch integration."""
from local_coder.context.compression import (
    COMPACTION_NOTICE,
    SUMMARY_PREFIX,
    compaction_fidelity,
    compress_messages,
    message_size,
)
from local_coder.types import Message, ToolCall


def test_existing_summary_avoids_duplicate_notice_and_retains_durable_facts():
    messages = [
        Message(role="system", content="Host policy"),
        Message(role="user", content="Review the parser"),
        Message(role="user", content=SUMMARY_PREFIX + "Earlier observations."),
        Message(role="assistant", content="Constraint: Preserve the public API.\n" + "old prose " * 100),
        Message(role="assistant", content="latest prose " * 70),
    ]
    compacted = compress_messages(messages, 1000)
    assert compacted != messages
    assert all(isinstance(message, Message) for message in compacted)
    contents = "\n".join(message.content for message in compacted)
    assert "Earlier tool history was compacted" not in contents
    assert contents.count(SUMMARY_PREFIX) == 1
    assert "Constraint: Preserve the public API." in contents
    assert compaction_fidelity(messages, contents)["passed"]
    assert sum(message_size(message) for message in compacted) <= 1000


def test_repeated_fallback_compaction_does_not_grow_notices_or_lose_pinned_evidence():
    pinned = "Pinned notes (kept verbatim through context compaction):\nF1 big.py:2 Command injection | argv[1] -> os.system"
    messages = [Message(role="system", content="Host policy"),
                Message(role="user", content="Review"),
                Message(role="system", content=COMPACTION_NOTICE),
                Message(role="user", content=pinned)]
    for index in range(3):
        call = ToolCall(id=str(index), name="read_file", arguments={"path": "big.py"})
        messages.extend([Message(role="assistant", content="", tool_calls=[call]),
                         Message(role="tool", tool_call_id=call.id, name=call.name, content="x = 1\n" * 1000)])
        messages = compress_messages(messages, 1800)
        assert sum(message.content.startswith(COMPACTION_NOTICE) for message in messages) == 1
        assert any(message.content == pinned for message in messages)
        assert sum(message_size(message) for message in messages) <= 1800

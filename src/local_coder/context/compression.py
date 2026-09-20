"""Bounded conversation compression for long-running coding sessions."""
from __future__ import annotations

from local_coder.types import Message


def compress_messages(messages: list[Message], max_chars: int) -> list[Message]:
    """Keep system/task context and the newest messages within a character budget."""
    if max_chars <= 0 or sum(len(message.content) for message in messages) <= max_chars:
        return messages

    if not messages:
        return []

    preserved = [message for message in messages[:2] if message.role in {"system", "user"}]
    remaining = max_chars - sum(len(message.content) for message in preserved)
    recent: list[Message] = []
    for message in reversed(messages[2:]):
        if remaining <= 0:
            break
        content = message.content
        if len(content) > remaining:
            content = content[-remaining:]
        recent.append(message.model_copy(update={"content": content}))
        remaining -= len(content)

    summary = Message(
        role="system",
        content="Earlier tool history was compacted to stay within the context budget.",
    )
    return preserved + [summary] + list(reversed(recent))
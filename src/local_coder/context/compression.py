"""Bounded history compression that preserves complete tool exchanges."""
from __future__ import annotations

from local_coder.types import Message


def message_size(message: Message) -> int:
    return len(message.model_dump_json())


def compress_messages(messages: list[Message], max_chars: int) -> list[Message]:
    if max_chars <= 0 or sum(message_size(m) for m in messages) <= max_chars or not messages:
        return messages
    preserved = [m for m in messages[:2] if m.role in {"system", "user"}]
    groups = []
    index = 2
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":  # No matching assistant in this history.
            index += 1
            continue
        group = [message]
        index += 1
        if message.tool_calls:
            expected = {call.id for call in message.tool_calls}
            while index < len(messages) and messages[index].role in {"tool", "system"}:
                group.append(messages[index])
                index += 1
            if expected != {m.tool_call_id for m in group if m.role == "tool"}:
                continue
        groups.append(group)
    remaining = max_chars - sum(message_size(m) for m in preserved)
    selected = []
    for group in reversed(groups):
        size = sum(message_size(m) for m in group)
        if selected and size > remaining:
            break
        if size > remaining:
            # Preserve the latest complete exchange and unmodified reasoning.
            # Limit large tool text; never cut calls or create orphan results.
            allowance = max(64, remaining // max(1, len(group)))
            group = [m.model_copy(update={"content": m.content[-allowance:]})
                     if len(m.content) > allowance else m for m in group]
        selected.append(group)
        remaining -= sum(message_size(m) for m in group)
    summary = Message(role="system", content="Earlier tool history was compacted to stay within the context budget.")
    return preserved + [summary] + [m for group in reversed(selected) for m in group]

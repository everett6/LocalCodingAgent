"""Deterministic compaction: preserve instructions and complete tool exchanges.

Only tool *text* is shortened. Assistant calls and provider reasoning remain
byte-exact. An optional archive callback retains recoverable originals on disk.
"""
from __future__ import annotations

from collections.abc import Callable
from local_coder.types import Message


def message_size(message: Message) -> int:
    return len(message.model_dump_json())


def compress_messages(messages: list[Message], max_chars: int,
                      archive: Callable[[list[Message]], str] | None = None) -> list[Message]:
    if max_chars <= 0 or sum(message_size(m) for m in messages) <= max_chars or not messages:
        return messages
    preserved = [m for m in messages[:2] if m.role in {"system", "user"}]
    groups = []
    index = 2
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
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
    reference = archive(messages) if archive else None
    notice = "Earlier tool history was compacted to stay within the context budget."
    if reference:
        if reference.startswith("agent-memory:"):
            notice += f" Full untrusted history retained by the embedding agent: {reference}."
        else:
            notice += f" Full untrusted history is recoverable with recover_context using reference {reference}."
    summary = Message(role="system", content=notice)
    remaining = max_chars - sum(message_size(m) for m in preserved + [summary])
    selected = []
    for group in reversed(groups):
        size = sum(message_size(m) for m in group)
        if selected and size > remaining:
            break
        if size > remaining:
            # Never modify provider reasoning or assistant tool-call arguments.
            fixed = sum(message_size(m) for m in group if m.role != "tool")
            tools = [m for m in group if m.role == "tool"]
            allowance = max(64, (remaining - fixed) // max(1, len(tools)) - 200)
            compacted = []
            for m in group:
                if m.role == "tool" and len(m.content) > allowance:
                    marker = "[Tool text compacted; " + (f"full history: {reference}" if reference else "earlier text omitted") + "]\n"
                    half = max(0, (allowance - len(marker)) // 2)
                    content = marker + (m.content[:half] + "\n…\n" + m.content[-half:] if half else "")
                    m = m.model_copy(update={"content": content})
                compacted.append(m)
            group = compacted
        selected.append(group)
        remaining -= sum(message_size(m) for m in group)
    return preserved + [summary] + [m for group in reversed(selected) for m in group]

"""Bounded conversation compression for long-running coding sessions.

Three layers, cheapest first, modelled on how opencode keeps long sessions
inside a model's window:

1. truncate_tool_output(): a single huge tool result (a test log, a
   recursive listing) never enters the conversation whole -- only its head
   and tail do, with a pointer to the full text on disk.
2. prune_tool_outputs(): once the conversation is over budget, old tool
   results are cleared in place (a stale read of a file that was read again
   later first, then anything older than the protected recent window). No
   model call, and the assistant/tool message structure stays intact.
3. summarize_history(): if pruning wasn't enough, the model itself writes a
   summary of the middle of the conversation, which replaces it.
   compress_messages() is the no-model fallback when that fails.

All three only rewrite history when the budget is actually exceeded. Every
rewrite changes the prompt prefix and so throws away the local server's KV
cache from that point on; doing it rarely, and all at once, keeps prefix
reuse working for the iterations in between.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from local_coder.types import Message

PRUNED_TOOL_OUTPUT = "[Old tool output cleared to save context. Re-run the tool if you need it again.]"
SUPERSEDED_TOOL_OUTPUT = "[Tool output cleared: the same call was repeated later; see the newer result.]"
SUMMARY_PREFIX = "Summary of earlier work (older messages were compacted to save context):\n"

# Tools whose result depends only on their arguments and current repository
# state, so an identical later call makes an earlier result redundant.
IDEMPOTENT_TOOLS = frozenset({
    "read_file", "list_files", "search_files", "grep",
    "git_status", "git_diff", "git_log",
})

SUMMARY_SYSTEM_PROMPT = (
    "You compress a coding agent's working history so it can continue the task from your "
    "summary alone. Write a concise, factual summary covering: what has been done so far; "
    "which files were read or changed, with the specific facts learned from them (names, "
    "signatures, line numbers that matter); commands and tests run and their outcome; "
    "errors still unresolved; and what remains to do next. Do not invent anything and do "
    "not include large code blocks unless they are essential to continue."
)


def message_chars(messages: list[Message]) -> int:
    return sum(len(message.content) for message in messages)


def truncate_tool_output(
    text: str,
    max_chars: int,
    max_lines: int,
    spill_path: Path | None = None,
    spill_display: str | None = None,
) -> str:
    """Return text unchanged if it fits, else its head and tail.

    Errors and test failures tend to sit at the end of an output and the
    command's framing at the start, so both ends are kept (60/40) and the
    middle is dropped. When spill_path is given the full text is written
    there and the notice tells the model how to read the omitted part.
    """
    lines = text.splitlines()
    if len(text) <= max_chars and len(lines) <= max_lines:
        return text

    head_budget = int(max_chars * 0.6)
    tail_budget = max_chars - head_budget
    head_lines_max = int(max_lines * 0.6)
    tail_lines_max = max_lines - head_lines_max

    head: list[str] = []
    used = 0
    for line in lines:
        if len(head) >= head_lines_max or used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1

    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head):]):
        if len(tail) >= tail_lines_max or used + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()

    # A single line longer than the whole budget (minified JS, a base64
    # blob) would otherwise leave both ends empty.
    if not head and not tail and lines:
        head = [lines[0][:head_budget]]
        tail = [lines[-1][-tail_budget:]] if len(lines) > 1 else []

    omitted_lines = max(len(lines) - len(head) - len(tail), 0)
    omitted_chars = max(len(text) - sum(len(line) + 1 for line in head + tail), 0)
    notice = f"... [{omitted_lines} lines / {omitted_chars} chars omitted"
    if spill_path is not None:
        try:
            spill_path.parent.mkdir(parents=True, exist_ok=True)
            # O_NOFOLLOW: the spill file sits in the user's checkout, and a
            # symlink planted there must not redirect this write elsewhere.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(spill_path, flags, 0o600), "w", encoding="utf-8") as f:
                f.write(text)
            notice += (
                f"; full output saved to {spill_display or spill_path} -- use read_file with "
                "start_line/end_line, or grep, to see the omitted part"
            )
        except OSError:
            pass
    notice += "] ..."
    return "\n".join(head + [notice] + tail)


def _head_end(messages: list[Message]) -> int:
    """Index of the first message after the task preamble (the system
    prompt, the user's task, and any system notes before the first model
    turn). The preamble is never compacted."""
    for index, message in enumerate(messages):
        if message.role in {"assistant", "tool"}:
            return index
    return len(messages)


def _tail_start(messages: list[Message], head_end: int, keep_chars: int) -> int:
    """Start of the newest run of messages fitting in keep_chars, moved
    forward so it never begins with a tool result whose assistant tool call
    would be cut off (OpenAI-compatible servers reject an orphaned tool
    message, and a model can't make sense of one either)."""
    start = len(messages)
    used = 0
    while start > head_end and used + len(messages[start - 1].content) <= keep_chars:
        start -= 1
        used += len(messages[start].content)
    while start < len(messages) and messages[start].role == "tool":
        start += 1
    return start


def compress_messages(messages: list[Message], max_chars: int) -> list[Message]:
    """Keep system/task context and the newest messages within a character budget."""
    if max_chars <= 0 or message_chars(messages) <= max_chars:
        return messages

    if not messages:
        return []

    preserved = [message for message in messages[:2] if message.role in {"system", "user"}]
    remaining = max_chars - message_chars(preserved)
    recent: list[Message] = []
    for message in reversed(messages[2:]):
        if remaining <= 0:
            break
        content = message.content
        if len(content) > remaining:
            content = content[-remaining:]
        recent.append(message.model_copy(update={"content": content}))
        remaining -= len(content)
    recent.reverse()
    while recent and recent[0].role == "tool":
        recent.pop(0)

    summary = Message(
        role="system",
        content="Earlier tool history was compacted to stay within the context budget.",
    )
    return preserved + [summary] + recent


def _tool_call_signatures(messages: list[Message]) -> dict[str, str]:
    """Map tool_call_id -> "name:canonical-args" for idempotent tool calls."""
    signatures: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls or []:
            if call.name in IDEMPOTENT_TOOLS:
                signatures[call.id] = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
    return signatures


def prune_tool_outputs(messages: list[Message], protect_chars: int) -> tuple[list[Message], int]:
    """Clear old tool results in place. Returns (messages, chars freed).

    First, results of an idempotent call that was repeated identically
    later are cleared anywhere in the history. Then, walking back from the
    newest message, tool results beyond the first protect_chars of tool
    output are cleared too. Message count, roles and tool_call ids are
    untouched so the history stays well-formed.
    """
    signatures = _tool_call_signatures(messages)
    seen_later: set[str] = set()
    protected = 0
    freed = 0
    result = list(messages)
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if message.role != "tool" or message.content in {PRUNED_TOOL_OUTPUT, SUPERSEDED_TOOL_OUTPUT}:
            continue
        signature = signatures.get(message.tool_call_id or "")
        replacement = None
        if signature is not None and signature in seen_later:
            replacement = SUPERSEDED_TOOL_OUTPUT
        elif protected + len(message.content) <= protect_chars:
            protected += len(message.content)
        else:
            # Once one result falls outside the window, everything older
            # does too, so later small ones can't sneak back in and leave
            # a patchwork the model has to reason around.
            protected = protect_chars + 1
            replacement = PRUNED_TOOL_OUTPUT
        if signature is not None:
            seen_later.add(signature)
        if replacement is not None and len(replacement) < len(message.content):
            freed += len(message.content) - len(replacement)
            result[index] = message.model_copy(update={"content": replacement})
    return result, freed


def _render_transcript(messages: list[Message], per_message_chars: int) -> str:
    parts = []
    for message in messages:
        content = message.content
        if len(content) > per_message_chars:
            content = content[:per_message_chars] + f"\n...[{len(content) - per_message_chars} chars omitted]"
        if message.role == "tool":
            parts.append(f"[tool result: {message.name or 'tool'}]\n{content}")
        elif message.tool_calls:
            calls = "; ".join(
                f"{call.name}({json.dumps(call.arguments, default=str)[:300]})" for call in message.tool_calls
            )
            parts.append(f"[assistant]\n{content}\n[tool calls] {calls}".strip())
        else:
            parts.append(f"[{message.role}]\n{content}")
    return "\n\n".join(parts)


def _strip_think(text: str) -> str:
    """Qwen3-style reasoning models may wrap their thinking in <think> tags;
    only the answer belongs in the summary."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


async def summarize_history(
    model,
    messages: list[Message],
    keep_recent_chars: int,
    max_input_chars: int,
    max_summary_tokens: int = 1024,
):
    """Replace the middle of the conversation with a model-written summary.

    Returns (new_messages, model_response), or None when there is nothing
    to summarize or the model produced nothing usable -- the caller then
    falls back to compress_messages().
    """
    head_end = _head_end(messages)
    tail_start = _tail_start(messages, head_end, keep_recent_chars)
    middle = messages[head_end:tail_start]
    if not middle:
        return None

    # Drop our own earlier summary's prefix so it's folded in, not nested.
    previous = [
        message.model_copy(update={"content": message.content.removeprefix(SUMMARY_PREFIX)})
        if message.content.startswith(SUMMARY_PREFIX) else message
        for message in middle
    ]
    transcript = _render_transcript(previous, per_message_chars=max(max_input_chars // 8, 500))
    if len(transcript) > max_input_chars:
        transcript = "...[earliest history omitted]\n" + transcript[-max_input_chars:]

    head = messages[:head_end]
    task = next((message.content for message in head if message.role == "user"), "")
    prompt = (
        f"## Task\n{task[:2000]}\n\n## History to summarize\n{transcript}\n\n"
        "Write the summary now."
    )
    response = await model.generate(
        [Message(role="system", content=SUMMARY_SYSTEM_PROMPT), Message(role="user", content=prompt)],
        temperature=0.1,
        max_tokens=max_summary_tokens,
    )
    summary = _strip_think(response.content or "")
    if not summary:
        return None
    return head + [Message(role="user", content=SUMMARY_PREFIX + summary)] + messages[tail_start:], response

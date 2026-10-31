"""Bounded, event-loop-local evidence channels for one host-created task plan.

Messages are reference data. No message kind approves tools, changes scheduling,
widens file scope, or impersonates the human supervisor. Nothing is persisted.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field
import json
import math
import re
from typing import Literal
from uuid import uuid4


MessageKind = Literal["status", "evidence", "blocked", "question"]
_KINDS = frozenset({"status", "evidence", "blocked", "question"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
_ASSIGNMENT = re.compile(
    r"(?im)((?:api[_-]?key|password|passwd|secret|token|client[_-]?secret|access[_-]?token|auth[_-]?token)"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s\n]+)"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_BEARER = re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)[^\s\"']+")
_PREFIXED_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MailboxError(ValueError):
    """Invalid, unavailable, or over-budget plan-local message operation."""


class MailboxClosed(MailboxError):
    """The owning plan ended; its channels can no longer be used."""


def _bounded_integer(value: int, lower: int, upper: int, label: str) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise MailboxError(f"{label} must be an integer from {lower} to {upper}")
    return value


@dataclass(frozen=True)
class TeamMessage:
    run_id: str
    sequence: int
    sender: str
    recipient: str
    kind: MessageKind
    content: str
    untrusted: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Inbox:
    # A deque with an asyncio notification is a nonblocking bounded channel.
    # Only PlanMailbox appends, after checking explicit capacity; no silent drops.
    pending: deque[TeamMessage] = field(default_factory=deque)
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class PlanMailbox:
    """One broker per plan, with host-issued participants and lifetime quotas.

    Use from the owning event loop. ``send`` rejects a full channel immediately;
    ``drain`` never waits. ``receive`` is an optional bounded host-side wait.
    Lifetime quotas include drained messages, preventing unlimited message churn.
    """

    def __init__(
        self, participant_ids: list[str] | tuple[str, ...], *,
        max_pending: int = 16, max_messages: int = 256,
        max_message_chars: int = 2000, max_total_bytes: int = 128_000,
        secrets: list[str] | tuple[str, ...] = (),
    ):
        if (not isinstance(participant_ids, (list, tuple)) or not 1 <= len(participant_ids) <= 64
                or any(not isinstance(name, str) or not _ID.fullmatch(name) for name in participant_ids)
                or len(set(participant_ids)) != len(participant_ids)):
            raise MailboxError("Participants must be 1 to 64 unique host-issued task IDs")
        self.max_pending = _bounded_integer(max_pending, 1, 64, "Pending capacity")
        self.max_messages = _bounded_integer(max_messages, 1, 4096, "Message quota")
        self.max_message_chars = _bounded_integer(max_message_chars, 1, 8000, "Message character limit")
        self.max_total_bytes = _bounded_integer(max_total_bytes, 1024, 1_048_576, "Total byte quota")
        if (not isinstance(secrets, (list, tuple)) or len(secrets) > 64
                or any(not isinstance(value, str) or not 1 <= len(value) <= 4096 for value in secrets)):
            raise MailboxError("Known secret filters must be a bounded host-supplied list")
        self.run_id = uuid4().hex
        self.participants = tuple(participant_ids)
        self._inboxes = {name: _Inbox() for name in self.participants}
        self._secrets = tuple(sorted(set(secrets), key=len, reverse=True))
        self._sent = 0
        self._bytes = 0
        self._closed = False

    def _inbox(self, participant: str) -> _Inbox:
        if self._closed:
            raise MailboxClosed("Plan messaging is closed")
        if not isinstance(participant, str) or participant not in self._inboxes:
            raise MailboxError("Task is not a participant in this plan")
        return self._inboxes[participant]

    def _filter(self, content: str) -> str:
        for secret in self._secrets:
            content = content.replace(secret, "[redacted]")
        content = _PRIVATE_KEY.sub("[redacted]", content)
        content = _BEARER.sub(r"\1[redacted]", content)
        content = _PREFIXED_SECRET.sub("[redacted]", content)
        content = _ASSIGNMENT.sub(r"\1[redacted]", content)
        return _CONTROL.sub("\ufffd", content)

    async def send(self, sender: str, recipient: str, kind: MessageKind, content: str) -> TeamMessage:
        self._inbox(sender)
        inbox = self._inbox(recipient)
        if not isinstance(kind, str) or kind not in _KINDS:
            raise MailboxError("Message kind must be status, evidence, blocked, or question")
        if not isinstance(content, str) or not content.strip() or len(content) > self.max_message_chars:
            raise MailboxError("Message content must be nonempty bounded text")
        content = self._filter(content)
        if len(content) > self.max_message_chars:
            raise MailboxError("Filtered message exceeds the character limit")
        if len(inbox.pending) >= self.max_pending:
            raise MailboxError("Recipient mailbox is full; message was not sent")
        if self._sent >= self.max_messages:
            raise MailboxError("Plan message quota is exhausted; message was not sent")
        message = TeamMessage(self.run_id, self._sent + 1, sender, recipient, kind, content)
        size = len(json.dumps(message.as_dict(), ensure_ascii=False).encode())
        if self._bytes + size > self.max_total_bytes:
            raise MailboxError("Plan byte quota is exhausted; message was not sent")
        # No await between capacity validation and append: concurrent actors on
        # the same event loop cannot race these quotas or reorder a send.
        inbox.pending.append(message)
        self._sent += 1
        self._bytes += size
        inbox.ready.set()
        return message

    def drain(self, recipient: str, *, limit: int = 8, char_budget: int = 4000) -> list[TeamMessage]:
        inbox = self._inbox(recipient)
        _bounded_integer(limit, 1, 16, "Read limit")
        _bounded_integer(char_budget, 256, 32_000, "Read character budget")
        result = []
        while inbox.pending and len(result) < limit:
            candidate = inbox.pending[0]
            length = len(json.dumps([*(row.as_dict() for row in result), candidate.as_dict()], ensure_ascii=False))
            if length > char_budget:
                if not result:
                    raise MailboxError("Read budget cannot fit the next message; nothing was consumed")
                break
            result.append(inbox.pending.popleft())
        if not inbox.pending:
            inbox.ready.clear()
        return result

    async def receive(self, recipient: str, *, timeout: float = 5) -> TeamMessage | None:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise MailboxError("Receive timeout must be greater than zero and at most 30 seconds")
        inbox = self._inbox(recipient)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self._inbox(recipient)
            if inbox.pending:
                result = inbox.pending.popleft()
                if not inbox.pending:
                    inbox.ready.clear()
                return result
            inbox.ready.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(inbox.ready.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    def describe(self) -> dict:
        return {"run_id": self.run_id, "participants": list(self.participants),
                "sent": self._sent, "pending": sum(len(inbox.pending) for inbox in self._inboxes.values()),
                "remaining_messages": self.max_messages - self._sent,
                "remaining_bytes": self.max_total_bytes - self._bytes, "closed": self._closed}

    def close(self) -> None:
        self._closed = True
        self._secrets = ()
        for inbox in self._inboxes.values():
            inbox.pending.clear()
            inbox.ready.set()

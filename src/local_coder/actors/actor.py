"""Actor model framework for multi-agent delegation.

Provides an actor system where each actor has an isolated mailbox and
processes messages sequentially. Actors can spawn child actors, send
messages to peers, request human approval, and signal execution blocks.

This is intentionally lightweight: no external actor framework dependency,
just asyncio queues and tasks. It builds on the existing DAG scheduler and
event bus rather than replacing them.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional


class ActorState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class ActorMessage:
    """A message sent between actors."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    sender: str = ""
    recipient: str = ""
    type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    reply_to: str | None = None
    requires_approval: bool = False
    # Approval decision: None = pending, True = approved, False = denied
    approval_decision: bool | None = None


@dataclass
class ActorRef:
    """A reference to an actor, used for sending messages."""
    actor_id: str
    actor_type: str
    system: "ActorSystem"

    async def send(self, message: ActorMessage) -> None:
        """Send a message to this actor."""
        message.recipient = self.actor_id
        await self.system.send(message)

    async def ask(self, message: ActorMessage, timeout: float = 30.0) -> ActorMessage:
        """Send a message and wait for a reply."""
        reply_future: asyncio.Future[ActorMessage] = asyncio.get_event_loop().create_future()
        message.reply_to = f"reply-{uuid.uuid4().hex[:8]}"
        self.system._pending_replies[message.reply_to] = reply_future
        await self.send(message)
        try:
            return await asyncio.wait_for(reply_future, timeout=timeout)
        except asyncio.TimeoutError:
            self.system._pending_replies.pop(message.reply_to, None)
            raise


class Actor:
    """Base actor class. Subclasses implement handle_message."""

    def __init__(self, actor_id: str, actor_type: str, system: "ActorSystem"):
        self.actor_id = actor_id
        self.actor_type = actor_type
        self.system = system
        self.state = ActorState.IDLE
        self._mailbox: asyncio.Queue[ActorMessage] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._children: set[str] = set()
        self._parent: str | None = None
        self._approval_callback: Optional[Callable[[str, dict[str, Any]], asyncio.Future[bool]]] = None
        self._metadata: dict[str, Any] = {}
        self._pending_approval_message: ActorMessage | None = None

    @property
    def ref(self) -> ActorRef:
        return ActorRef(actor_id=self.actor_id, actor_type=self.actor_type, system=self.system)

    async def start(self) -> None:
        """Start the actor's message processing loop."""
        if self._task is not None:
            return
        self.system._actors[self.actor_id] = self
        self._stopped = False
        self.state = ActorState.IDLE
        self._task = asyncio.create_task(self._run_loop(), name=f"actor-{self.actor_id}")

    async def stop(self) -> None:
        """Stop the actor and wait for its loop to finish."""
        self._stopped = True
        self.system._actors.pop(self.actor_id, None)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.state = ActorState.STOPPED

    async def send(self, message: ActorMessage) -> None:
        """Send a message to this actor's mailbox (internal delivery)."""
        await self._mailbox.put(message)

    async def _run_loop(self) -> None:
        """Main message processing loop."""
        while not self._stopped:
            try:
                message = await asyncio.wait_for(self._mailbox.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            try:
                # Approval decisions bypass normal handling and unblock a
                # waiting actor directly.
                if message.type in {"approval_granted", "approval_denied"}:
                    if self.state == ActorState.WAITING_APPROVAL:
                        message.approval_decision = message.type == "approval_granted"
                        await self._resolve_pending_approval(message)
                    continue

                self.state = ActorState.RUNNING
                await self.handle_message(message)
                self.state = ActorState.IDLE
            except ApprovalRequired as exc:
                self.state = ActorState.WAITING_APPROVAL
                # Emit an approval request event
                await self.system.emit_event(
                    source=self.actor_id,
                    event_type="approval_required",
                    message=str(exc),
                    data={"actor_id": self.actor_id, "message_id": message.id, "payload": exc.payload},
                    task_id=message.task_id,
                )
                # Store the original message so we can replay it after approval.
                self._pending_approval_message = message
                # Do not re-queue; wait for an explicit approval_granted/denied.
            except Exception as exc:
                self.state = ActorState.FAILED
                await self.system.emit_event(
                    source=self.actor_id,
                    event_type="actor_failed",
                    message=str(exc),
                    data={"actor_id": self.actor_id, "message_id": message.id},
                    task_id=message.task_id,
                )
                raise

    async def _resolve_pending_approval(self, message: ActorMessage) -> None:
        """Replay the message that was waiting for approval."""
        original = getattr(self, "_pending_approval_message", None)
        if original is None:
            return
        self._pending_approval_message = None
        if not message.approval_decision:
            # Denied: surface the denial and do not replay the original.
            self.state = ActorState.IDLE
            await self.system.emit_event(
                source=self.actor_id,
                event_type="approval_denied",
                message=f"Approval denied for {original.type}",
                data={"actor_id": self.actor_id, "message_id": original.id, "payload": original.payload},
                task_id=original.task_id,
            )
            return
        # Granted: replay the original message.
        self.state = ActorState.RUNNING
        try:
            await self.handle_message(original)
            self.state = ActorState.IDLE
        except ApprovalRequired as exc:
            self.state = ActorState.WAITING_APPROVAL
            await self.system.emit_event(
                source=self.actor_id,
                event_type="approval_required",
                message=str(exc),
                data={"actor_id": self.actor_id, "message_id": original.id, "payload": exc.payload},
                task_id=original.task_id,
            )
            self._pending_approval_message = original
        except Exception as exc:
            self.state = ActorState.FAILED
            await self.system.emit_event(
                source=self.actor_id,
                event_type="actor_failed",
                message=str(exc),
                data={"actor_id": self.actor_id, "message_id": original.id},
                task_id=original.task_id,
            )
            raise

    async def handle_message(self, message: ActorMessage) -> None:
        """Handle an incoming message. Subclasses override this."""
        raise NotImplementedError

    async def spawn_child(self, actor_type: str, actor_id: str | None = None, **kwargs) -> "ActorRef":
        """Spawn a child actor."""
        child = self.system.spawn(actor_type, actor_id, parent=self.actor_id, **kwargs)
        await child.start()
        self._children.add(child.actor_id)
        return child.ref

    async def request_approval(self, description: str, payload: dict[str, Any] | None = None) -> bool:
        """Request human approval for an action. Returns True if approved."""
        if self._approval_callback is None:
            raise ApprovalRequired(
                f"Actor {self.actor_id} requires approval: {description}",
                payload=payload or {},
            )
        future = self._approval_callback(description, payload or {})
        if future is None:
            raise ApprovalRequired(
                f"Actor {self.actor_id} requires approval: {description}",
                payload=payload or {},
            )
        return await future

    def set_approval_callback(
        self, callback: Callable[[str, dict[str, Any]], asyncio.Future[bool]] | None
    ) -> None:
        """Set the human approval callback."""
        self._approval_callback = callback

    def set_metadata(self, key: str, value: Any) -> None:
        self._metadata[key] = value

    def get_metadata(self, key: str, default: Any = None) -> Any:
        return self._metadata.get(key, default)


class ApprovalRequired(Exception):
    """Raised when an actor needs human approval before proceeding."""

    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


class ActorSystem:
    """Manages actors, message routing, and lifecycle."""

    def __init__(self):
        self._actors: dict[str, Actor] = {}
        self._pending_replies: dict[str, asyncio.Future[ActorMessage]] = {}
        self._event_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._lock = asyncio.Lock()

    def spawn(
        self,
        actor_type: str,
        actor_id: str | None = None,
        parent: str | None = None,
        **kwargs,
    ) -> ActorRef:
        """Spawn a new actor of the given type."""
        actor_id = actor_id or f"{actor_type}-{uuid.uuid4().hex[:8]}"
        if actor_id in self._actors:
            raise ValueError(f"Actor {actor_id} already exists")

        actor = self._create_actor(actor_type, actor_id, **kwargs)
        actor._parent = parent
        self._actors[actor_id] = actor
        return actor.ref

    def _create_actor(self, actor_type: str, actor_id: str, **kwargs) -> Actor:
        """Create an actor instance by type."""
        if actor_type == "coordinator":
            from local_coder.actors.coordinator_actor import CoordinatorActor
            return CoordinatorActor(actor_id, actor_type, self, **kwargs)
        if actor_type == "coder":
            from local_coder.actors.coder_actor import CoderActor
            return CoderActor(actor_id, actor_type, self, **kwargs)
        if actor_type == "tester":
            from local_coder.actors.tester_actor import TesterActor
            return TesterActor(actor_id, actor_type, self, **kwargs)
        if actor_type == "reviewer":
            from local_coder.actors.reviewer_actor import ReviewerActor
            return ReviewerActor(actor_id, actor_type, self, **kwargs)
        raise ValueError(f"Unknown actor type: {actor_type}")

    def get_actor(self, actor_id: str) -> Optional[Actor]:
        return self._actors.get(actor_id)

    async def send(self, message: ActorMessage) -> None:
        """Route a message to the recipient actor."""
        recipient = self._actors.get(message.recipient)
        if recipient is None:
            # If no recipient specified, broadcast to all actors
            for actor in self._actors.values():
                await actor.send(message)
            return
        await recipient.send(message)

    async def emit_event(self, **kwargs) -> None:
        """Emit an event to all registered handlers."""
        for handler in self._event_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(kwargs)
                else:
                    handler(kwargs)
            except Exception:
                pass  # Event handler failures should not break actors

    def on_event(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Register an event handler."""
        self._event_handlers.append(handler)

    async def resolve_reply(self, reply_to: str, message: ActorMessage) -> None:
        """Resolve a pending reply future."""
        future = self._pending_replies.pop(reply_to, None)
        if future is not None and not future.done():
            future.set_result(message)

    async def grant_approval(self, actor_id: str) -> bool:
        """Grant a pending approval request for an actor, unblocking it."""
        actor = self._actors.get(actor_id)
        if actor is None or actor.state != ActorState.WAITING_APPROVAL:
            return False
        await actor.send(
            ActorMessage(
                sender="supervisor",
                recipient=actor_id,
                type="approval_granted",
                payload={},
            )
        )
        return True

    async def deny_approval(self, actor_id: str) -> bool:
        """Deny a pending approval request for an actor."""
        actor = self._actors.get(actor_id)
        if actor is None or actor.state != ActorState.WAITING_APPROVAL:
            return False
        await actor.send(
            ActorMessage(
                sender="supervisor",
                recipient=actor_id,
                type="approval_denied",
                payload={},
            )
        )
        return True

    async def stop_all(self) -> None:
        """Stop all actors."""
        for actor in list(self._actors.values()):
            await actor.stop()

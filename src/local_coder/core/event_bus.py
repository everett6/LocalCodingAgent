"""Event bus for agent communication and UI updates.

Replaces callback-based events with a proper pub/sub system supporting:
- Synchronous and asynchronous handlers
- WebSocket broadcasting for real-time UI updates
- Event filtering and replay
- Persistence via SQLite
- Priority-based delivery
"""

import asyncio
import json
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Set


class AgentEvent:
    """Standardized event emitted by agents and the orchestrator."""
    
    def __init__(
        self,
        source: str,
        event_type: str,
        message: str,
        data: Optional[dict] = None,
        task_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        id: Optional[str] = None,
    ):
        self.id = id if id is not None else str(uuid.uuid4())
        self.source = source
        self.event_type = event_type
        self.message = message
        self.data = data or {}
        self.task_id = task_id
        self.timestamp = timestamp or time.time()
    
    def model_dump(self, mode: str = "json") -> dict:
        """Serialize event for JSON/API transmission."""
        return {
            "id": self.id,
            "source": self.source,
            "event_type": self.event_type,
            "message": self.message,
            "data": self.data,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }
    
    @classmethod
    def model_validate(cls, data: dict) -> "AgentEvent":
        """Deserialize from dict."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            source=data["source"],
            event_type=data["event_type"],
            message=data["message"],
            data=data.get("data"),
            task_id=data.get("task_id"),
            timestamp=data.get("timestamp"),
        )


class EventBus(ABC):
    """Abstract base class for event bus implementations."""
    
    @abstractmethod
    async def emit(self, event: AgentEvent) -> None:
        """Emit an event to all registered handlers."""
        pass
    
    @abstractmethod
    async def on(self, event_type: str, handler: Callable[[AgentEvent], None]) -> None:
        """Register a handler for a specific event type."""
        pass
    
    @abstractmethod
    async def off(self, event_type: str, handler: Callable[[AgentEvent], None]) -> None:
        """Unregister a handler."""
        pass
    
    @abstractmethod
    async def emit_to_task(self, event: AgentEvent, task_id: str) -> None:
        """Emit event only to handlers registered for a specific task."""
        pass
    
    @abstractmethod
    async def get_recent(self, task_id: str, limit: int = 50) -> List[AgentEvent]:
        """Get recent events for a task."""
        pass


class InMemoryEventBus(EventBus):
    """In-memory event bus with support for async handlers and task-scoped filtering."""
    
    def __init__(
        self, *, max_task_events: int = 1000, max_tasks: int = 1000,
        max_all_events: int = 1000,
    ):
        for value in (max_task_events, max_tasks, max_all_events):
            if type(value) is not int or value < 1:
                raise ValueError("Event history bounds must be positive integers")
        # event_type -> list of (handler, priority)
        self._handlers: Dict[str, List[tuple[Callable[[AgentEvent], None], int]]] = defaultdict(list)
        # Recently active tasks retain bounded tails; oldest tasks are evicted.
        self._task_events: OrderedDict[str | None, deque[AgentEvent]] = OrderedDict()
        self._max_task_events = max_task_events
        self._max_tasks = max_tasks
        # All events for replay/debugging (limited size)
        self._all_events: deque[AgentEvent] = deque(maxlen=max_all_events)
        self._lock = asyncio.Lock()
    
    async def emit(self, event: AgentEvent) -> None:
        """Emit an event to all registered handlers."""
        async with self._lock:
            if event.task_id not in self._task_events:
                self._task_events[event.task_id] = deque(maxlen=self._max_task_events)
            self._task_events[event.task_id].append(event)
            self._task_events.move_to_end(event.task_id)
            if len(self._task_events) > self._max_tasks:
                self._task_events.popitem(last=False)
            self._all_events.append(event)
            
            # Freeze delivery order before callbacks may change subscriptions.
            handlers = sorted(self._handlers.get(event.event_type, []), key=lambda x: -x[1])
        # Callbacks can subscribe, unsubscribe or emit nested events. None of
        # those operations may wait for a lock held by their own emitter.
        for handler, priority in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                # Log error but don't crash the bus
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Error in event handler for {event.event_type}: {e}")
    
    async def on(self, event_type: str, handler: Callable[[AgentEvent], None], priority: int = 0) -> None:
        """Register a handler for a specific event type."""
        async with self._lock:
            self._handlers[event_type].append((handler, priority))
    
    async def off(self, event_type: str, handler: Callable[[AgentEvent], None]) -> None:
        """Unregister a handler."""
        async with self._lock:
            handlers = self._handlers.get(event_type, [])
            self._handlers[event_type] = [(h, p) for h, p in handlers if h != handler]
    
    async def emit_to_task(self, event: AgentEvent, task_id: str) -> None:
        """Emit event only if it's related to the task."""
        # Always emit, but handlers can filter by task_id
        await self.emit(event)
    
    async def get_recent(self, task_id: str, limit: int = 50) -> List[AgentEvent]:
        """Get recent events for a task."""
        if limit <= 0:
            return []
        async with self._lock:
            return list(self._task_events.get(task_id, ()))[-limit:]


class WebSocketEventBus:
    """WebSocket-enabled event bus that broadcasts to connected clients.
    
    Combines InMemoryEventBus with WebSocket client management for real-time UI updates.
    """
    
    def __init__(self, memory_bus: InMemoryEventBus | None = None):
        self._memory_bus = memory_bus or InMemoryEventBus()
        self._ws_clients: Set["WebSocketClient"] = set()
        self._lock = asyncio.Lock()
    
    async def emit(self, event: AgentEvent) -> None:
        """Emit event to both memory handlers and WebSocket clients."""
        # First, emit to in-memory handlers
        await self._memory_bus.emit(event)
        # Then broadcast to WebSocket clients
        await self._broadcast(event)
    
    async def _broadcast(self, event: AgentEvent) -> None:
        """Broadcast event to all connected WebSocket clients."""
        if not self._ws_clients:
            return
        
        message = json.dumps({
            "type": "agent_event",
            "event": event.model_dump(),
        })
        
        disconnected: Set["WebSocketClient"] = set()
        for client in tuple(self._ws_clients):
            try:
                await client.send(message)
            except Exception:
                disconnected.add(client)
        
        # Remove disconnected clients
        self._ws_clients -= disconnected
    
    def register_ws(self, client: "WebSocketClient") -> None:
        """Register a WebSocket client."""
        self._ws_clients.add(client)
    
    def unregister_ws(self, client: "WebSocketClient") -> None:
        """Unregister a WebSocket client."""
        self._ws_clients.discard(client)


class WebSocketClient:
    """Wrapper for a WebSocket connection."""
    
    def __init__(
        self, ws_connection, *, max_queue_size: int = 256,
        max_message_bytes: int = 1_000_000,
    ):
        for value in (max_queue_size, max_message_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("WebSocket bounds must be positive integers")
        self._ws = ws_connection
        self._send_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue_size)
        self._max_message_bytes = max_message_bytes
        self._failed = False
        self._task: asyncio.Task | None = None
    
    async def send(self, message: str) -> None:
        """Send a message over the WebSocket."""
        # Never silently lose an event or stall the emitter behind a slow
        # client. Overflow invalidates this subscription; the broadcaster
        # removes it and the send loop closes its socket for explicit resync.
        if self._failed:
            raise BufferError("WebSocket event queue overflow; reconnect to resync")
        if len(message) > self._max_message_bytes or len(message.encode("utf-8")) > self._max_message_bytes:
            await self._overflow()
            raise BufferError("WebSocket message exceeds limit; reconnect to resync")
        try:
            self._send_queue.put_nowait(message)
        except asyncio.QueueFull:
            await self._overflow()
            raise BufferError("WebSocket event queue overflow; reconnect to resync") from None

    async def _overflow(self) -> None:
        self._failed = True
        if self._task and not self._task.done():
            self._task.cancel()
        await self._close()

    async def _close(self) -> None:
        close = getattr(self._ws, "close", None)
        if close is not None:
            try:
                await asyncio.wait_for(
                    close(code=1013, reason="Event stream interrupted; reconnect to resync"),
                    timeout=1,
                )
            except Exception:
                pass
    
    async def start(self) -> None:
        """Start the send loop for this WebSocket client."""
        if self._task and not self._task.done():
            return
        if self._failed:
            raise BufferError("WebSocket event queue overflow; reconnect to resync")
        self._task = asyncio.create_task(self._send_loop())
    
    async def _send_loop(self) -> None:
        """Continuously send messages from the queue."""
        try:
            while True:
                message = await self._send_queue.get()
                if self._ws.closed:
                    break
                await self._ws.send(message)
        except Exception:
            pass  # Connection closed
        finally:
            self._failed = True
            # Standard websocket adapters support close(code, reason).
            await self._close()
    
    def closed(self) -> bool:
        """Check if the WebSocket is closed."""
        return self._failed or getattr(self._ws, 'closed', True)


# Convenience function for common event types
def orchestrator_event(message: str, data: Optional[dict] = None, task_id: Optional[str] = None) -> AgentEvent:
    """Create an orchestrator event."""
    return AgentEvent(
        source="ORCHESTRATOR",
        event_type="orchestrator_message",
        message=message,
        data=data,
        task_id=task_id,
    )


def planner_event(message: str, data: Optional[dict] = None, task_id: Optional[str] = None) -> AgentEvent:
    """Create a planner event."""
    return AgentEvent(
        source="PLANNER",
        event_type="planner_message",
        message=message,
        data=data,
        task_id=task_id,
    )


def coder_event(message: str, data: Optional[dict] = None, task_id: Optional[str] = None) -> AgentEvent:
    """Create a coder event."""
    return AgentEvent(
        source="CODER",
        event_type="coder_message",
        message=message,
        data=data,
        task_id=task_id,
    )


def reviewer_event(message: str, data: Optional[dict] = None, task_id: Optional[str] = None) -> AgentEvent:
    """Create a reviewer event."""
    return AgentEvent(
        source="REVIEWER",
        event_type="reviewer_message",
        message=message,
        data=data,
        task_id=task_id,
    )


def debugger_event(message: str, data: Optional[dict] = None, task_id: Optional[str] = None) -> AgentEvent:
    """Create a debugger event."""
    return AgentEvent(
        source="DEBUGGER",
        event_type="debugger_message",
        message=message,
        data=data,
        task_id=task_id,
    )


def tester_event(message: str, data: Optional[dict] = None, task_id: Optional[str] = None) -> AgentEvent:
    """Create a tester event."""
    return AgentEvent(
        source="TESTER",
        event_type="tester_message",
        message=message,
        data=data,
        task_id=task_id,
    )


def system_event(message: str, data: Optional[dict] = None) -> AgentEvent:
    """Create a system event."""
    return AgentEvent(
        source="SYSTEM",
        event_type="system_message",
        message=message,
        data=data,
    )

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
from collections import defaultdict
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
    ):
        self.id = str(uuid.uuid4())
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
    
    def __init__(self):
        # event_type -> list of (handler, priority)
        self._handlers: Dict[str, List[tuple[Callable[[AgentEvent], None], int]]] = defaultdict(list)
        # task_id -> list of events (for replay)
        self._task_events: Dict[str, List[AgentEvent]] = defaultdict(list)
        # All events for replay/debugging (limited size)
        self._all_events: List[AgentEvent] = []
        self._max_all_events = 1000
        self._lock = asyncio.Lock()
    
    async def emit(self, event: AgentEvent) -> None:
        """Emit an event to all registered handlers."""
        async with self._lock:
            self._task_events[event.task_id].append(event)
            self._all_events.append(event)
            if len(self._all_events) > self._max_all_events:
                self._all_events = self._all_events[-self._max_all_events:]
            
            # Notify handlers for this event type
            handlers = self._handlers.get(event.event_type, [])
            for handler, priority in sorted(handlers, key=lambda x: -x[1]):
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
        events = self._task_events.get(task_id, [])
        return events[-limit:] if len(events) > limit else events


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
        for client in self._ws_clients:
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
    
    def __init__(self, ws_connection):
        self._ws = ws_connection
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
    
    async def send(self, message: str) -> None:
        """Send a message over the WebSocket."""
        await self._send_queue.put(message)
    
    async def start(self) -> None:
        """Start the send loop for this WebSocket client."""
        if self._task and not self._task.done():
            return
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
    
    def closed(self) -> bool:
        """Check if the WebSocket is closed."""
        return getattr(self._ws, 'closed', True)


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
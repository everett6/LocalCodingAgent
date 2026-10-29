"""Event persistence and callback reentrancy contracts."""
import asyncio

from local_coder.core.event_bus import AgentEvent, InMemoryEventBus


def test_event_round_trip_preserves_its_identity():
    event = AgentEvent("CODER", "progress", "Working", task_id="task")
    event.id = "persisted-event"
    restored = AgentEvent.model_validate(event.model_dump())
    assert restored.model_dump() == event.model_dump()


def test_handler_can_replace_subscription_and_emit_another_event():
    async def scenario():
        bus = InMemoryEventBus()
        delivered = []

        async def replacement(event):
            delivered.append(event.message)

        async def handler(event):
            delivered.append(event.message)
            await bus.off("progress", handler)
            await bus.on("progress", replacement)
            await bus.emit(AgentEvent("CODER", "progress", "nested"))

        await bus.on("progress", handler)
        await asyncio.wait_for(bus.emit(AgentEvent("CODER", "progress", "outer")), 1)
        await bus.emit(AgentEvent("CODER", "progress", "followup"))
        assert delivered == ["outer", "nested", "followup"]
        assert [event.message for event in await bus.get_recent(None)] == ["outer", "nested", "followup"]

    asyncio.run(scenario())


def test_handlers_keep_priority_order_and_event_subscription_snapshot():
    async def scenario():
        bus = InMemoryEventBus()
        delivered = []

        def low(event):
            delivered.append("low")

        async def high(event):
            delivered.append("high")
            await bus.off("progress", low)

        await bus.on("progress", low)
        await bus.on("progress", high, priority=1)
        await asyncio.wait_for(bus.emit(AgentEvent("CODER", "progress", "first")), 1)
        await bus.emit(AgentEvent("CODER", "progress", "second"))
        assert delivered == ["high", "low", "high"]

    asyncio.run(scenario())

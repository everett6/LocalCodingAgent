"""Tests for the actor model framework."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from local_coder.actors.actor import Actor, ActorMessage, ActorSystem, ActorState, ApprovalRequired
from local_coder.actors.coordinator_actor import CoordinatorActor
from local_coder.actors.coder_actor import CoderActor
from local_coder.types import AgentRole, AgentTask, TaskPlan, TaskStatus


class TestActorMessage:
    def test_message_creation(self):
        msg = ActorMessage(sender="a", recipient="b", type="test", payload={"key": "val"})
        assert msg.sender == "a"
        assert msg.recipient == "b"
        assert msg.type == "test"
        assert msg.payload == {"key": "val"}
        assert msg.id != ""


class TestActorSystem:
    def test_spawn_and_get_actor(self):
        system = ActorSystem()
        ref = system.spawn("coder", actor_id="coder-1")
        assert ref.actor_id == "coder-1"
        assert system.get_actor("coder-1") is not None

    def test_spawn_duplicate_raises(self):
        system = ActorSystem()
        system.spawn("coder", actor_id="coder-1")
        with pytest.raises(ValueError):
            system.spawn("coder", actor_id="coder-1")

    def test_spawn_unknown_type_raises(self):
        system = ActorSystem()
        with pytest.raises(ValueError):
            system.spawn("unknown_type")

    @pytest.mark.asyncio
    async def test_send_and_receive(self):
        system = ActorSystem()
        received = []

        class TestActor(Actor):
            async def handle_message(self, message: ActorMessage) -> None:
                received.append(message)

        actor = TestActor("test-1", "test", system)
        await actor.start()
        try:
            await actor.send(ActorMessage(sender="x", recipient="test-1", type="ping", payload={}))
            await asyncio.sleep(0.1)
            assert len(received) == 1
            assert received[0].type == "ping"
        finally:
            await actor.stop()

    @pytest.mark.asyncio
    async def test_ask_and_reply(self):
        system = ActorSystem()

        class ReplierActor(Actor):
            async def handle_message(self, message: ActorMessage) -> None:
                if message.reply_to:
                    await system.resolve_reply(
                        message.reply_to,
                        ActorMessage(sender=self.actor_id, recipient=message.sender, type="pong", payload={"ok": True}),
                    )

        replier = ReplierActor("replier", "test", system)
        await replier.start()
        try:
            reply = await replier.ref.ask(ActorMessage(sender="asker", recipient="replier", type="ping", payload={}))
            assert reply.type == "pong"
            assert reply.payload == {"ok": True}
        finally:
            await replier.stop()

    @pytest.mark.asyncio
    async def test_approval_required(self):
        system = ActorSystem()

        class ApprovalActor(Actor):
            async def handle_message(self, message: ActorMessage) -> None:
                await self.request_approval("Do something", payload={"action": "delete"})

        actor = ApprovalActor("approver", "test", system)
        await actor.start()
        try:
            await actor.send(ActorMessage(sender="x", recipient="approver", type="do", payload={}))
            await asyncio.sleep(0.1)
            assert actor.state.name == "WAITING_APPROVAL"
        finally:
            await actor.stop()


class TestCoordinatorActor:
    def test_coordinator_creation(self):
        system = ActorSystem()
        ref = system.spawn("coordinator", actor_id="coord-1", project_root=".")
        assert ref.actor_id == "coord-1"

    @pytest.mark.asyncio
    async def test_coordinator_execute_plan(self):
        system = ActorSystem()
        coordinator = CoordinatorActor("coord-1", "coordinator", system, project_root=".")
        await coordinator.start()

        try:
            plan = TaskPlan(
                objective="Test plan",
                tasks=[
                    AgentTask(
                        task_id="task-1",
                        role=AgentRole.CODER,
                        objective="Write code",
                        depends_on=[],
                    ),
                ],
            )

            # Mock the child actor execution
            with patch.object(coordinator, "_execute_task", new=AsyncMock()) as mock_exec:
                await coordinator.handle_message(
                    ActorMessage(
                        sender="user",
                        type="execute_plan",
                        payload={"plan": plan.model_dump(mode="json")},
                    )
                )
                await asyncio.sleep(0.2)
                mock_exec.assert_called_once()
        finally:
            await coordinator.stop()


class TestTaskActor:
    @pytest.mark.asyncio
    async def test_task_actor_execute(self):
        system = ActorSystem()
        mock_model_manager = AsyncMock()
        mock_model_manager.get_model.return_value = AsyncMock()
        mock_tool_registry = AsyncMock()
        mock_dispatch = AsyncMock()

        actor = CoderActor(
            "coder-1", "coder", system,
            project_root=".",
            config=None,
            model_manager=mock_model_manager,
            tool_registry=mock_tool_registry,
            dispatch=mock_dispatch,
        )
        await actor.start()

        try:
            task = AgentTask(
                task_id="task-1",
                role=AgentRole.CODER,
                objective="Write hello world",
            )

            # Mock create_agent to return a fake agent
            fake_agent = AsyncMock()
            fake_agent.execute.return_value = AsyncMock()
            fake_agent.execute.return_value.task_id = "task-1"
            fake_agent.execute.return_value.status = TaskStatus.COMPLETED
            fake_agent.execute.return_value.summary = "Done"
            fake_agent.execute.return_value.files_changed = []
            fake_agent.execute.return_value.tests_run = []
            fake_agent.execute.return_value.tests_passed = True
            fake_agent.execute.return_value.issues = []

            with patch("local_coder.actors.task_actor.create_agent", return_value=fake_agent):
                reply = await actor.ref.ask(
                    ActorMessage(
                        sender="coord",
                        recipient="coder-1",
                        type="execute_task",
                        payload={"task": task.model_dump(mode="json")},
                    ),
                    timeout=10.0,
                )

            assert reply.type == "task_completed"
            assert reply.payload["summary"] == "Done"
        finally:
            await actor.stop()

    @pytest.mark.asyncio
    async def test_actor_approval_grant_unblocks(self):
        """An actor waiting for approval resumes when approval is granted."""
        system = ActorSystem()

        class ApprovalActor(Actor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.handle_count = 0

            async def handle_message(self, message: ActorMessage) -> None:
                self.handle_count += 1
                if self.handle_count == 1:
                    # First time: request approval
                    await self.request_approval("Do something", payload={"action": "delete"})
                # Second time (after approval granted): complete normally

        actor = ApprovalActor("approver", "test", system)
        await actor.start()
        try:
            await actor.send(ActorMessage(sender="x", recipient="approver", type="do", payload={}))
            await asyncio.sleep(0.2)
            assert actor.state == ActorState.WAITING_APPROVAL

            # Grant approval
            granted = await system.grant_approval("approver")
            assert granted is True
            await asyncio.sleep(0.2)
            assert actor.state != ActorState.WAITING_APPROVAL
            assert actor.handle_count == 2
        finally:
            await actor.stop()

    @pytest.mark.asyncio
    async def test_actor_approval_deny(self):
        """An actor waiting for approval is notified when approval is denied."""
        system = ActorSystem()

        class ApprovalActor(Actor):
            async def handle_message(self, message: ActorMessage) -> None:
                await self.request_approval("Do something", payload={"action": "delete"})

        actor = ApprovalActor("approver2", "test", system)
        await actor.start()
        try:
            await actor.send(ActorMessage(sender="x", recipient="approver2", type="do", payload={}))
            await asyncio.sleep(0.2)
            assert actor.state == ActorState.WAITING_APPROVAL

            # Deny approval
            denied = await system.deny_approval("approver2")
            assert denied is True
            await asyncio.sleep(0.2)
            assert actor.state != ActorState.WAITING_APPROVAL
        finally:
            await actor.stop()

"""Base task actor that delegates to a local_coder agent."""

from __future__ import annotations

from local_coder.agents import create_agent
from local_coder.actors.actor import Actor, ActorMessage, ApprovalRequired
from local_coder.types import AgentRole, AgentTask, AgentResponse, TaskStatus


class TaskActor(Actor):
    """Base actor that wraps a local_coder agent for task execution."""

    def __init__(self, actor_id: str, actor_type: str, system, **kwargs):
        super().__init__(actor_id, actor_type, system)
        self._project_root = kwargs.get("project_root", ".")
        self._config = kwargs.get("config")
        self._model_manager = kwargs.get("model_manager")
        self._tool_registry = kwargs.get("tool_registry")
        self._dispatch = kwargs.get("dispatch")
        self._assigned_task: AgentTask | None = None
        self._agent = None

    async def handle_message(self, message: ActorMessage) -> None:
        if message.type == "execute_task":
            task_data = message.payload.get("task")
            if task_data is None:
                await self._reply(message, {"error": "No task data"}, success=False)
                return
            self._assigned_task = AgentTask.model_validate(task_data)
            await self._execute_task(message)
        else:
            raise ValueError(f"Unknown message type: {message.type}")

    async def _execute_task(self, message: ActorMessage) -> None:
        if self._assigned_task is None:
            await self._reply(message, {"error": "No task assigned"}, success=False)
            return

        role = self._assigned_task.role
        model = await self._model_manager.get_model(role) if self._model_manager else None

        context_window = 180000
        compact_context = 90000
        if self._config is not None:
            context_window = self._config.agentic.context_window_chars
            compact_context = self._config.agentic.compact_context_chars

        agent = create_agent(
            role,
            model,
            self._tool_registry,
            self._dispatch,
            context_window_chars=context_window,
            compact_context_chars=compact_context,
        )

        if agent is None:
            await self._reply(message, {"error": f"Could not create agent for role {role}"}, success=False)
            return

        self._agent = agent

        try:
            response = await agent.execute(self._assigned_task)
            await self._reply(
                message,
                {
                    "task_id": response.task_id,
                    "status": response.status.value,
                    "summary": response.summary,
                    "files_changed": response.files_changed,
                    "tests_run": response.tests_run,
                    "tests_passed": response.tests_passed,
                    "issues": response.issues,
                },
                success=response.status == TaskStatus.COMPLETED,
            )
        except ApprovalRequired:
            raise
        except Exception as exc:
            await self._reply(message, {"error": str(exc)}, success=False)

    async def _reply(self, message: ActorMessage, payload: dict, success: bool) -> None:
        reply_type = "task_completed" if success else "task_failed"
        if message.reply_to:
            await self.system.resolve_reply(
                message.reply_to,
                ActorMessage(
                    sender=self.actor_id,
                    recipient=message.sender,
                    type=reply_type,
                    payload=payload,
                    task_id=message.task_id,
                ),
            )
        else:
            await self.system.send(
                ActorMessage(
                    sender=self.actor_id,
                    recipient=message.sender,
                    type=reply_type,
                    payload=payload,
                    task_id=message.task_id,
                )
            )

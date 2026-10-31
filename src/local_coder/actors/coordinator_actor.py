"""Coordinator actor that plans and delegates work to child actors."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from local_coder.actors.actor import Actor, ActorMessage, ActorState, ApprovalRequired
from local_coder.types import AgentRole, AgentTask, TaskPlan, TaskStatus


class CoordinatorActor(Actor):
    """Coordinates multi-agent work by spawning child actors and managing dependencies."""

    def __init__(self, actor_id: str, actor_type: str, system, **kwargs):
        super().__init__(actor_id, actor_type, system)
        self._project_root = kwargs.get("project_root", ".")
        self._config = kwargs.get("config")
        self._model_manager = kwargs.get("model_manager")
        self._tool_registry = kwargs.get("tool_registry")
        self._dispatch = kwargs.get("dispatch")
        self._active_plans: dict[str, dict[str, Any]] = {}

    async def handle_message(self, message: ActorMessage) -> None:
        if message.type == "execute_plan":
            await self._handle_execute_plan(message)
        elif message.type == "task_completed":
            await self._handle_task_completed(message)
        elif message.type == "task_failed":
            await self._handle_task_failed(message)
        elif message.type == "request_approval":
            await self._handle_request_approval(message)
        else:
            raise ValueError(f"Unknown message type: {message.type}")

    async def _handle_execute_plan(self, message: ActorMessage) -> None:
        plan_data = message.payload.get("plan")
        if plan_data is None:
            raise ValueError("execute_plan requires a 'plan' payload")

        plan = TaskPlan(**plan_data) if isinstance(plan_data, dict) else plan_data
        plan_id = plan.plan_id

        self._active_plans[plan_id] = {
            "plan": plan,
            "completed": set(),
            "failed": set(),
            "results": {},
            "parent_sender": message.sender,
            "task_id": message.task_id,
        }

        # Emit plan created event
        await self.system.emit_event(
            source=self.actor_id,
            event_type="plan_created",
            message=f"Plan {plan_id}: {len(plan.tasks)} task(s)",
            data={
                "plan_id": plan_id,
                "tasks": [
                    {"task_id": t.task_id, "role": t.role.value, "objective": t.objective, "depends_on": t.depends_on}
                    for t in plan.tasks
                ],
            },
            task_id=message.task_id,
        )

        # Start executing ready tasks
        await self._execute_ready_tasks(plan_id)

    async def _execute_ready_tasks(self, plan_id: str) -> None:
        plan_info = self._active_plans.get(plan_id)
        if plan_info is None:
            return

        plan = plan_info["plan"]
        completed = plan_info["completed"]
        failed = plan_info["failed"]

        # Find ready tasks
        ready = []
        for task in plan.tasks:
            if task.task_id in completed or task.task_id in failed:
                continue
            if all(dep in completed for dep in task.depends_on):
                ready.append(task)

        if not ready:
            # Check if plan is done
            all_done = all(t.task_id in completed or t.task_id in failed for t in plan.tasks)
            if all_done:
                await self._complete_plan(plan_id)
            return

        # Execute ready tasks in parallel (bounded by config)
        max_parallel = 1
        if self._config is not None:
            max_parallel = max(1, self._config.agentic.max_parallel_agents)

        semaphore = asyncio.Semaphore(max_parallel)
        await asyncio.gather(*(
            self._execute_task(plan_id, task, semaphore) for task in ready
        ))

    async def _execute_task(self, plan_id: str, task: AgentTask, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            plan_info = self._active_plans.get(plan_id)
            if plan_info is None:
                return

            # Spawn a child actor for this task
            child = await self.spawn_child(
                actor_type=task.role.value,
                actor_id=f"task-{task.task_id}",
                project_root=self._project_root,
                config=self._config,
                model_manager=self._model_manager,
                tool_registry=self._tool_registry,
                dispatch=self._dispatch,
                task=task,
            )

            await self.system.emit_event(
                source=self.actor_id,
                event_type="task_started",
                message=f"Task {task.task_id} ({task.role.value}) started",
                data={"task_id": task.task_id, "role": task.role.value, "actor_id": child.actor_id},
                task_id=task.task_id,
            )

            # Send execute message to child
            try:
                reply = await child.ask(
                    ActorMessage(
                        sender=self.actor_id,
                        type="execute_task",
                        payload={"task": task.model_dump(mode="json")},
                        task_id=task.task_id,
                    ),
                    timeout=300.0,
                )

                if reply.type == "task_completed":
                    plan_info["completed"].add(task.task_id)
                    plan_info["results"][task.task_id] = reply.payload
                    await self.system.emit_event(
                        source=self.actor_id,
                        event_type="task_completed",
                        message=f"Task {task.task_id} completed",
                        data={"task_id": task.task_id, "result": reply.payload},
                        task_id=task.task_id,
                    )
                else:
                    plan_info["failed"].add(task.task_id)
                    await self.system.emit_event(
                        source=self.actor_id,
                        event_type="task_failed",
                        message=f"Task {task.task_id} failed",
                        data={"task_id": task.task_id, "error": reply.payload.get("error", "unknown")},
                        task_id=task.task_id,
                    )
            except ApprovalRequired as exc:
                # Forward approval request to the human supervisor
                await self.system.emit_event(
                    source=self.actor_id,
                    event_type="approval_required",
                    message=str(exc),
                    data={"actor_id": child.actor_id, "payload": exc.payload},
                    task_id=task.task_id,
                )
                # Don't mark as failed; the message will be retried after approval
                # For now, mark as blocked
                plan_info["failed"].add(task.task_id)
            except Exception as exc:
                plan_info["failed"].add(task.task_id)
                await self.system.emit_event(
                    source=self.actor_id,
                    event_type="task_failed",
                    message=f"Task {task.task_id} errored: {exc}",
                    data={"task_id": task.task_id, "error": str(exc)},
                    task_id=task.task_id,
                )

            # Continue with next ready tasks
            await self._execute_ready_tasks(plan_id)

    async def _handle_task_completed(self, message: ActorMessage) -> None:
        task_id = message.payload.get("task_id")
        plan_id = message.payload.get("plan_id")
        if plan_id and task_id:
            plan_info = self._active_plans.get(plan_id)
            if plan_info:
                plan_info["completed"].add(task_id)
                plan_info["results"][task_id] = message.payload.get("result", {})
                await self._execute_ready_tasks(plan_id)

    async def _handle_task_failed(self, message: ActorMessage) -> None:
        task_id = message.payload.get("task_id")
        plan_id = message.payload.get("plan_id")
        if plan_id and task_id:
            plan_info = self._active_plans.get(plan_id)
            if plan_info:
                plan_info["failed"].add(task_id)
                await self._execute_ready_tasks(plan_id)

    async def _handle_request_approval(self, message: ActorMessage) -> None:
        # Forward to human supervisor via event
        await self.system.emit_event(
            source=self.actor_id,
            event_type="approval_required",
            message=message.payload.get("description", "Approval required"),
            data=message.payload,
            task_id=message.task_id,
        )

    async def _complete_plan(self, plan_id: str) -> None:
        plan_info = self._active_plans.pop(plan_id, None)
        if plan_info is None:
            return

        plan = plan_info["plan"]
        all_completed = all(t.task_id in plan_info["completed"] for t in plan.tasks)
        has_failures = len(plan_info["failed"]) > 0

        await self.system.emit_event(
            source=self.actor_id,
            event_type="plan_completed",
            message=f"Plan {plan_id} {'completed' if all_completed and not has_failures else 'finished with failures'}",
            data={
                "plan_id": plan_id,
                "all_completed": all_completed,
                "has_failures": has_failures,
                "completed": list(plan_info["completed"]),
                "failed": list(plan_info["failed"]),
                "results": plan_info["results"],
            },
            task_id=plan_info["task_id"],
        )

        # Notify parent sender
        if plan_info["parent_sender"]:
            parent = self.system.get_actor(plan_info["parent_sender"])
            if parent:
                await parent.send(
                    ActorMessage(
                        sender=self.actor_id,
                        type="plan_completed",
                        payload={
                            "plan_id": plan_id,
                            "all_completed": all_completed,
                            "has_failures": has_failures,
                            "results": plan_info["results"],
                        },
                        task_id=plan_info["task_id"],
                    )
                )

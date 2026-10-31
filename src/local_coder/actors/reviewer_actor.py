"""Reviewer actor for code review tasks."""

from local_coder.actors.task_actor import TaskActor
from local_coder.types import AgentRole


class ReviewerActor(TaskActor):
    """Actor that handles code review tasks."""

    def __init__(self, actor_id: str, actor_type: str, system, **kwargs):
        super().__init__(actor_id, actor_type, system, **kwargs)
        self.role = AgentRole.REVIEWER

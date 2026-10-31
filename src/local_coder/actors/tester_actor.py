"""Tester actor for test execution tasks."""

from local_coder.actors.task_actor import TaskActor
from local_coder.types import AgentRole


class TesterActor(TaskActor):
    """Actor that handles test execution tasks."""

    def __init__(self, actor_id: str, actor_type: str, system, **kwargs):
        super().__init__(actor_id, actor_type, system, **kwargs)
        self.role = AgentRole.TESTER

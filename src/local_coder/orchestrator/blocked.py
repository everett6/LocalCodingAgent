"""Terminal orchestration control for explicit agent refusal or scope blocks."""
from __future__ import annotations

from local_coder.types import AgentResponse, TaskStatus


class AgentBlocked(RuntimeError):
    """Carry the original BLOCKED response without treating refusal as failure.

    Callers may display ``response`` and record its blocked status. Retrying,
    escalating models or running downstream verification is not a resolution.
    """

    retryable = False

    def __init__(self, response: AgentResponse):
        if response.status != TaskStatus.BLOCKED:
            raise ValueError('AgentBlocked requires an explicitly blocked response')
        self.response = response
        super().__init__('Task blocked by an explicit refusal or authorization boundary.')

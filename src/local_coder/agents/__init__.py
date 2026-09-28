"""Agent package for local-coder."""
from __future__ import annotations
from typing import Callable

from local_coder.types import AgentRole, AgentEvent
from local_coder.agents.base import BaseAgent
from local_coder.agents.explorer import ExplorerAgent
from local_coder.agents.planner import PlannerAgent
from local_coder.agents.coder import CoderAgent
from local_coder.agents.debugger import DebuggerAgent
from local_coder.agents.tester import TesterAgent
from local_coder.agents.reviewer import ReviewerAgent
from local_coder.agents.security import SecurityAgent
from local_coder.agents.exploit_validator import ExploitValidatorAgent


def create_agent(
    role: AgentRole, 
    model, 
    tool_registry, 
    event_callback: Callable[[AgentEvent], None] | None = None,
    **kwargs,
) -> BaseAgent:
    """Factory function to create an agent instance based on role."""
    
    agent_map = {
        AgentRole.EXPLORER: ExplorerAgent,
        AgentRole.PLANNER: PlannerAgent,
        AgentRole.CODER: CoderAgent,
        AgentRole.DEBUGGER: DebuggerAgent,
        AgentRole.TESTER: TesterAgent,
        AgentRole.REVIEWER: ReviewerAgent,
        AgentRole.SECURITY: SecurityAgent,
        AgentRole.EXPLOIT_VALIDATOR: ExploitValidatorAgent,
    }
    
    agent_cls = agent_map.get(role)
    if not agent_cls:
        raise ValueError(f"Unknown agent role: {role}")
        
    return agent_cls(model, tool_registry, event_callback, **kwargs)

__all__ = [
    "BaseAgent",
    "ExplorerAgent",
    "PlannerAgent",
    "CoderAgent",
    "DebuggerAgent",
    "TesterAgent",
    "ReviewerAgent",
    "SecurityAgent",
    "ExploitValidatorAgent",
    "create_agent",
]

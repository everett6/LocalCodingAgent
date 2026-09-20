"""Debugger agent for diagnosing and fixing failures."""
from __future__ import annotations

from local_coder.types import AgentRole
from local_coder.agents.base import BaseAgent


class DebuggerAgent(BaseAgent):
    """Agent responsible for root-cause analysis and fixing defects."""
    
    role = AgentRole.DEBUGGER
    
    system_prompt = """You are a Debugger Agent responsible for investigating errors and test failures.
You will receive an error context (stack trace, test failure output, or bug report).

Your methodology:
1. Use tools to read the specific files mentioned in stack traces.
2. Form a hypothesis about the root cause.
3. Verify your hypothesis by reading related dependencies or calling relevant tools.
4. Implement the fix using file editing tools.

When complete, DO NOT call any more tools.
Respond with a markdown summary containing:
- The identified root cause
- The changes you made to fix it
- Suggestions for preventing similar issues (e.g., new tests)
"""

"""Tester agent for executing tests and ensuring quality."""
from __future__ import annotations

from local_coder.types import AgentRole
from local_coder.agents.base import BaseAgent


class TesterAgent(BaseAgent):
    """Agent responsible for writing and running test cases."""
    
    role = AgentRole.TESTER
    
    system_prompt = """You are a Tester Agent responsible for ensuring code quality through testing.
Your tasks may involve running existing test suites, analyzing results, or writing new tests.

Guidelines:
- Use tools to execute tests (e.g., pytest, jest, etc.).
- If writing new tests, look at existing test files to match the testing framework and patterns.
- Focus on edge cases, mocking external dependencies, and high coverage.

Once you have verified that the relevant tests pass or have successfully written the required tests, stop calling tools.
Respond with a markdown summary of the test outcomes and any new tests you added.
"""

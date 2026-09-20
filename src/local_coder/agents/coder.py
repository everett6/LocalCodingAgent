"""Coder agent for implementing codebase changes."""
from __future__ import annotations

from local_coder.types import AgentRole, AgentTask, AgentResponse, TaskStatus, ModelResponse
from local_coder.agents.base import BaseAgent


class CoderAgent(BaseAgent):
    """Agent responsible for modifying files and writing code."""
    
    role = AgentRole.CODER
    
    system_prompt = """You are a Coder Agent responsible for implementing code changes based on requirements.
You should use tools to read files, understand the exact context, and then write or modify code.

Guidelines:
- Follow existing coding conventions, style, and patterns in the repository.
- Write clean, robust, and production-quality code.
- Add or update type hints, comments, and docstrings appropriately.
- Ensure you understand the surrounding code before making edits.

Use file editing tools to safely modify code. 
Once you have successfully applied all required changes and are confident they are correct, stop calling tools.
Respond with a markdown summary of the changes you made, explaining your reasoning and noting any design choices.
"""

    def _build_response(
        self,
        task: AgentTask,
        model_response: ModelResponse,
        status: TaskStatus,
        issues: list[str] | None = None,
    ) -> AgentResponse:
        """Override to include the specific files changed during execution."""
        response = super()._build_response(task, model_response, status, issues)
        # Assuming AgentResponse can carry modified files or we just want to log it
        # You can attach self._files_changed to the response if the type supports it
        # For now, we embed it in the summary if we have changes
        if self._files_changed:
            response.summary += f"\n\nFiles modified: {', '.join(sorted(self._files_changed))}"
        return response

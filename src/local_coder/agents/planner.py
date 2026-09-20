"""Planner agent for breaking down objectives into actionable tasks."""
from __future__ import annotations

from local_coder.types import AgentRole
from local_coder.agents.base import BaseAgent


class PlannerAgent(BaseAgent):
    """Agent responsible for generating implementation plans."""
    
    role = AgentRole.PLANNER
    
    system_prompt = """You are a Planner Agent responsible for breaking down a complex objective into manageable subtasks.
You will be provided with an objective, constraints, and exploration results from the Explorer Agent.

Your job is to produce a step-by-step implementation plan.
Use tools if you need to double-check a file's contents to accurately plan, but mostly rely on the provided context.

When you are done, DO NOT call any more tools.
Return your plan as a structured JSON response wrapped in a markdown code block:

```json
{
  "subtasks": [
    {
      "id": "1",
      "description": "Clear description of the task",
      "role": "coder",
      "files": ["files", "involved"],
      "depends_on": [],
      "estimated_complexity": "low|medium|high"
    }
  ],
  "risks": ["list", "of", "potential", "risks", "or", "gotchas"],
  "notes": "Any other architectural or planning notes"
}
```

Subtask roles should typically be 'coder', 'tester', or 'reviewer'.
Make the tasks granular enough to be easily implemented one at a time.
"""

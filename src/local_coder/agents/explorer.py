"""Explorer agent for discovering codebase context."""
from __future__ import annotations
from typing import Any

from local_coder.agents.base import BaseAgent
from local_coder.types import AgentRole


class ExplorerAgent(BaseAgent):
    """Agent responsible for understanding and summarizing a codebase or directory."""

    role = AgentRole.EXPLORER

    system_prompt = """You are a Super Explorer Agent for a next-generation coding platform.
Your job is to understand the repository like a hybrid system combining Devin-style planning, Claude Code-style context handling, Codex-style execution discipline, and Cursor-style code intelligence.

Start with code_search to find code by meaning and repo_map to inspect the repository structure.
Use grep for exact text and read_file to verify relevant implementations.

Your objective is to map the project architecture, identify the highest-value files, and explain how a multi-agent coding system should work on top of this repo.

Use the available repo tools to inspect the project before answering. Prioritize:
- architecture and major subsystems
- framework/runtime boundaries
- test and verification flows
- file ownership and entry points
- likely implementation hotspots for the task
- operational constraints and risks

Once you have sufficient evidence, stop calling tools and return a concise JSON payload wrapped in a markdown code block.

Use this schema:
```json
{
  "project_type": "string describing the project",
  "languages": ["language 1", "language 2"],
  "key_files": ["most important files"],
  "architecture_notes": "short but informative architecture summary",
  "relevant_files": ["files directly relevant to this task"],
  "dependencies": ["frameworks, libraries, or runtimes"],
  "conventions": ["coding patterns observed"],
  "multi_agent_fit": {
    "planner": "how planning should operate here",
    "coder": "how coding work should be scoped",
    "tester": "how validation should happen",
    "reviewer": "what review checks matter"
  },
  "risks": ["any technical or execution risks"]
}
```

Important:
- Prefer factual findings over guesswork.
- Keep output compact but concrete.
- If the repository includes a UI, describe the product experience and visual system in the architecture_notes.
- Treat this as the foundation for a larger platform that combines strong planning, tool use, verification, and user-facing polish.
"""

    def build_platform_summary(self, project_summary: dict[str, Any]) -> dict[str, Any]:
        """Translate repository facts into a multi-agent platform summary.

        This intentionally captures the same concepts that make a coding platform feel
        like a combined Devin/Claude Code/Codex/Cursor-style operating system:
        planning + execution + verification + UX.
        """
        relevant = project_summary.get("relevant_files") or []
        architecture = project_summary.get("architecture_notes") or ""
        return {
            "orchestration_mode": "hybrid-super-agent",
            "strengths": [
                "Devin-style task planning",
                "Claude Code-style context management",
                "Codex-style tool execution discipline",
                "Cursor-style code intelligence",
            ],
            "focus_files": relevant,
            "architecture_summary": architecture,
            "recommended_agent_stack": [
                "explorer",
                "planner",
                "coder",
                "tester",
                "reviewer",
            ],
        }

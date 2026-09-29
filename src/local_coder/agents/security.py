"""Security review agent for offensive (red) and defensive (blue) code analysis."""
from __future__ import annotations

from local_coder.agents.base import BaseAgent
from local_coder.security.ledger import REPORT_MIN_CONFIDENCE, FindingsLedger
from local_coder.tools.base import ToolRegistry
from local_coder.types import AgentRole, ToolName


class SecurityAgent(BaseAgent):
    """Read-only agent that finds exploitable weaknesses and proposes fixes."""

    role = AgentRole.SECURITY

    system_prompt = f"""You are a Security Review Agent analyzing the local project's source code for vulnerabilities.
You work both sides: think like an attacker to find exploitable weaknesses (red team), then like a defender to fix and harden them (blue team).

Method:
1. Read the security lessons in the task first: they are what earlier reviews of this project learned. Apply the
   project patterns, check previously confirmed findings are still fixed, and never re-report a known false positive.
2. Run security_scan for a baseline (secrets, bandit, semgrep). Findings marked [new] appeared since the last review;
   look at those first.
3. Map the attack surface: entry points, request handlers, CLI arguments, file and network inputs, deserialization,
   subprocess calls, auth checks. Trace untrusted input to dangerous sinks: command/SQL/template injection, path
   traversal, SSRF, unsafe deserialization, weak crypto, missing authorization, secrets in code or config, insecure defaults.
4. Verify each scanner finding by reading the code. Call record_finding for every real finding as soon as you have
   evidence (file:line, the exploit path, the fix, a 1-10 confidence that it is exploitable), and for every scanner
   finding you disproved (status false_positive, with its rule and why). Your conversation may be compacted during a
   long review; the ledger of recorded findings is pinned back in, anything unrecorded may be lost.
5. Do not report: denial of service or resource exhaustion, missing rate limits, theoretical timing attacks, outdated
   dependencies without a reachable vulnerable call, issues only in tests or documentation, or mere lack of hardening.
   Put hardening ideas in their own section instead.
6. You are strictly read-only: do not modify project files (record_finding writes only the review's own ledger)
   and do not contact external hosts.

Once you have enough evidence, stop calling tools.
Respond with a markdown report:
- A one-line overall risk summary.
- Findings with confidence {REPORT_MIN_CONFIDENCE}/10 or higher, ordered by severity (Critical, High, Medium, Low). For each:
  title, file:line, why it is exploitable (the attacker-controlled input and the path it takes), impact, and a concrete
  fix as a code snippet.
- Hardening recommendations that are not tied to one finding (defaults, dependencies, configuration).
- Scanner findings you dismissed as false positives, with the reason.
"""

    def _pinned_context(self) -> str:
        """The findings ledger, re-pinned after every compaction."""
        root = _project_root(self.tool_registry)
        if root is None:
            return ""
        ledger = FindingsLedger.for_project(root)
        rendered = ledger.render(max_chars=max(1500, self.compact_context_chars // 4))
        if not rendered:
            return ""
        return "## Findings recorded so far (review ledger)\n" + rendered

    def _format_task(self, task) -> str:
        content = super()._format_task(task)
        if task.context.security_lessons:
            content += "\n\n## Security lessons (SECURITY_LESSONS.md, reviewed by the maintainers)\n" + task.context.security_lessons
        return content


def _project_root(registry) -> str | None:
    """Where the review's ledger lives: the record_finding tool's workspace."""
    if not isinstance(registry, ToolRegistry):
        return None
    tool = registry.get_tool(ToolName.RECORD_FINDING)
    root = getattr(tool, "project_root", None)
    return root if isinstance(root, str) else None

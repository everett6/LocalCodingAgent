"""Model-agnostic built-in workflows exposed as slash commands."""
from __future__ import annotations

from pathlib import Path

from local_coder.custom_commands import CustomCommand

_SKILLS = (
    (
        "/implement",
        "Implement a change with tests and verification",
        "<request>",
        "Implement this request: $ARGUMENTS\n\n"
        "First read applicable project instructions and inspect the relevant code and tests. "
        "Trace callers and existing patterns before editing. Make the smallest complete change, "
        "add or update regression tests, run the narrowest relevant checks, and inspect the final "
        "diff. Preserve unrelated work. Report changed files, verification results, and blockers.",
    ),
    (
        "/debug",
        "Find and fix a reproducible root cause",
        "<symptom>",
        "Debug this symptom: $ARGUMENTS\n\n"
        "Reproduce it first when practical. Inspect the failing path and callers, form competing "
        "hypotheses, and use evidence to narrow them down. Add a regression test for the root cause, "
        "make a minimal fix, then rerun the regression and relevant suite. Do not claim a fix or "
        "verification that was not observed.",
    ),
    (
        "/add-tests",
        "Add or improve tests without changing production code",
        "<behavior>",
        "Add tests for this behavior: $ARGUMENTS\n\n"
        "Inspect existing test conventions and production behavior. Change test files only; do not "
        "modify production code. Cover the normal case, meaningful boundaries, and a regression or "
        "failure case where appropriate. Run the new tests and report any behavior they reveal.",
    ),
    (
        "/refactor",
        "Refactor while preserving externally visible behavior",
        "<scope>",
        "Refactor this scope: $ARGUMENTS\n\n"
        "Inspect current behavior and relevant tests first. Preserve public behavior and APIs unless "
        "the request explicitly says otherwise. Avoid unrelated cleanup, keep the patch incremental, "
        "and run focused tests plus lint/type checks available in the project. Summarize any remaining "
        "risk or unverified behavior.",
    ),
    (
        "/handoff",
        "Create a concise, evidence-based continuation note",
        "[focus]",
        "Prepare a continuation handoff for this coding session. Focus, if supplied: $ARGUMENTS\n\n"
        "Summarize the objective, completed work, important files and symbols, decisions, commands "
        "and tests actually run with outcomes, current blockers, and the next concrete steps. Keep "
        "facts separate from hypotheses. Do not edit files or claim that work was saved beyond the "
        "existing session/checkpoint system.",
    ),
    (
        "/reflect",
        "Propose reusable lessons from the latest work",
        "[focus]",
        "Reflect on the latest completed work in this session. Focus, if supplied: $ARGUMENTS\n\n"
        "Identify only reusable workflow or repository lessons supported by observed evidence. "
        "Separate confirmed lessons from hypotheses, note the evidence and scope, and propose a "
        "short reusable instruction for each. Do not modify files or persist lessons; an operator "
        "will review candidates and may save them with /learn.",
    ),
)


def discover() -> list[CustomCommand]:
    """Return read-only built-in skills using the same argument expansion as custom commands."""
    source = Path(__file__).resolve()
    return [
        CustomCommand(
            name=name,
            template=template,
            path=source,
            scope="built-in skill",
            description=description,
            argument_hint=argument_hint,
        )
        for name, description, argument_hint, template in _SKILLS
    ]

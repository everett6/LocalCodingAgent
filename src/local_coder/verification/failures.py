"""Extract concise, actionable failure context from verification output."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Failure:
    test: str
    message: str
    location: str | None = None


def parse_failures(output: str, max_failures: int = 10) -> list[Failure]:
    """Parse pytest-style ``FAILED``/``ERROR`` headings and nearby diagnostic
    lines. Also reads the compact form produced by
    verification.test_runner.format_report, whose headings are followed by an
    ``  at <file:line>`` line."""
    lines = output.splitlines()
    failures: list[Failure] = []
    current: Failure | None = None

    for line in lines:
        heading = re.match(r"\s*(?:FAILED|ERROR)\s+(.+?)(?:\s+-\s+(.*))?$", line)
        if heading:
            current = Failure(heading.group(1), heading.group(2) or "test failed")
            failures.append(current)
            if len(failures) >= max_failures:
                break
            continue

        location = re.match(r"\s+at\s+(\S+:\d+|\S+)$", line)
        if current and location:
            failures[-1] = Failure(current.test, current.message, location.group(1))
            current = failures[-1]
            continue

        if current and line.strip() and not line.startswith("="):
            message = line.strip()
            if message.startswith(("E ", "AssertionError", "Error", "Exception")):
                failures[-1] = Failure(current.test, message, current.location)
                current = failures[-1]

    return failures


def summarize_failures(output: str, max_failures: int = 5) -> str:
    """Return only the relevant failure records for model context."""
    failures = parse_failures(output, max_failures=max_failures)
    if not failures:
        return output[-4000:]
    return "\n".join(
        f"- {f.test}: {f.message}" + (f" ({f.location})" if f.location else "")
        for f in failures
    )
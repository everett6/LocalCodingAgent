"""Extract concise, actionable failure context from verification output."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Failure:
    test: str
    message: str


def parse_failures(output: str, max_failures: int = 10) -> list[Failure]:
    """Parse common pytest failure headings and nearby diagnostic lines."""
    lines = output.splitlines()
    failures: list[Failure] = []
    current: Failure | None = None

    for line in lines:
        heading = re.search(r"FAILED\s+([^\s]+)(?:\s+-\s+(.*))?$", line)
        if heading:
            current = Failure(heading.group(1), heading.group(2) or "test failed")
            failures.append(current)
            if len(failures) >= max_failures:
                break
            continue

        if current and line.strip() and not line.startswith("="):
            message = line.strip()
            if message.startswith(("E ", "AssertionError", "Error", "Exception")):
                failures[-1] = Failure(current.test, message)
                current = failures[-1]

    return failures


def summarize_failures(output: str, max_failures: int = 5) -> str:
    """Return only the relevant failure records for model context."""
    failures = parse_failures(output, max_failures=max_failures)
    if not failures:
        return output[-4000:]
    return "\n".join(f"- {failure.test}: {failure.message}" for failure in failures)
"""Human-in-the-loop approval hook for risk-classified agent actions.

Tools consult an ApprovalCallback for anything CommandPolicy (or an
equivalent check) classifies as needing a human decision -- e.g. a shell
command that isn't clearly safe, or a git commit/checkout. The callback is
injected by whatever is driving the Coordinator: the interactive CLI prompts
the user, the remote control server and headless runs leave it unset (which
means "deny by default"), and tests can supply a fake.
"""
from __future__ import annotations

from typing import Awaitable, Callable

ApprovalCallback = Callable[[str], Awaitable[bool]]
"""async def callback(description: str) -> bool: True approves the action."""

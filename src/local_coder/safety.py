"""Command safety classification for local execution."""
from __future__ import annotations

import enum
import re


class CommandRisk(str, enum.Enum):
    SAFE = "safe"
    ASK = "ask"
    BLOCK = "block"


class CommandPolicy:
    """Classify commands before they reach a local shell."""

    BLOCK_PATTERNS = (
        r"(^|\s)rm\s+(-[rf]+\s+)*(/|~|\$HOME)(\s|$)",
        r"(^|\s)(sudo|su)\b",
        r"(^|\s)(mkfs|fdisk|parted)\b",
        r"(^|\s)dd\s+if=",
        r":\(\)\s*\{",
        r"(^|\s)(curl|wget)\b[^|\n]*\|\s*(sh|bash|zsh)\b",
    )
    ASK_PREFIXES = (
        "pip install", "pip3 install", "npm install", "yarn add", "pnpm add",
        "docker", "git commit", "git push", "git reset", "git checkout",
        "chmod", "chown", "curl", "wget",
    )
    SAFE_PREFIXES = (
        "pytest", "python", "python3", "npm test", "npm run test", "cargo test",
        "go test", "make test", "git status", "git diff", "git log", "ls", "pwd",
    )

    def classify(self, command: str) -> CommandRisk:
        normalized = command.strip()
        if not normalized:
            return CommandRisk.BLOCK
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in self.BLOCK_PATTERNS):
            return CommandRisk.BLOCK
        if normalized.startswith(self.ASK_PREFIXES):
            return CommandRisk.ASK
        if normalized.startswith(self.SAFE_PREFIXES):
            return CommandRisk.SAFE
        return CommandRisk.ASK
"""Observe assistant refusals without changing their meaning or overriding them.

This detector is deliberately narrow. Tool/user messages and quoted examples
are evidence, not refusals by the assistant. A detected refusal yields choices
for the human; it never yields a replacement prompt or a sampling retry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from local_coder.types import Message


@dataclass(frozen=True)
class RefusalDecision:
    reason: str = "The assistant declined this request; choose a permitted next step."

    @property
    def next_steps(self) -> tuple[dict[str, str], ...]:
        return (
            {"id": "local_static_analysis", "label": "Review authorized local source with static analysis"},
            {"id": "remediation", "label": "Focus on defensive remediation and regression tests"},
            {"id": "ask_scope", "label": "Clarify the permitted objective and scope"},
        )

    def to_event(self) -> dict:
        return {"classification": "assistant_refusal", "original_retained": True,
                "automatic_retry": False, "next_steps": list(self.next_steps)}


def _own_prose(text: str) -> str:
    """Remove common explicit quote/code forms before matching first-person prose."""
    text = re.sub(r"<(blockquote|pre|code)\b[^>]*>.*?</\1>", "", text, flags=re.I | re.S)
    lines = []
    fence = None
    attributed = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped:
            attributed = False
        if re.match(r"(?:tool (?:result|output)|source excerpt|quoted (?:text|reference)|example|fixture)\s*:", stripped, re.I):
            attributed = True
            continue
        if attributed:
            continue
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            kind = marker.group(1)[0]
            if fence is None:
                fence = kind
            elif fence == kind:
                fence = None
            continue
        if fence is not None or stripped.startswith(">") or line.startswith(("    ", "\t")):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r'`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|(?<!\w)\x27[^\x27\n]+\x27(?!\w)', "", text)
    return text.replace("’", "'")


def detect_refusal(message: Message) -> RefusalDecision | None:
    """Classify only an explicit refusal in the assistant's unquoted own text.

    Ordinary inability to fix a bug or access missing context remains eligible
    for the separate bounded, non-safety recovery loop.
    """
    if message.role != "assistant":
        return None
    prose = _own_prose(message.content)
    prefix = (r"(?:^|[.!?]\s+)\s*(?:(?:[-*]|\d+[.)])\s*)?(?:(?:i'm sorry|sorry|i apologize|apologies)[,.!]?(?:\s+but)?\s*)?"
              r"(?:as an ai(?: assistant| language model| model)?[, ]+)?"
              r"i(?:\s+(?:cannot|can't|won't|will not|am unable to)|'m unable to)\s+")
    patterns = (
        prefix + r"(?:help|assist|comply)\b",
        prefix + r"fulfill\s+(?:this|that|the|your)\s+request\b",
        prefix + r"(?:provide|generate|create|support)\b[^.\n]{0,240}\b(?:exploit|malware|harmful|unauthorized|credential|payload|bypass)\b",
        r"^\s*(?:this|that|your) request\s+(?:violates|is against)\s+(?:my |the )?(?:safety |security |usage )?policy\b",
    )
    return RefusalDecision() if any(re.search(pattern, prose, re.I | re.M) for pattern in patterns) else None

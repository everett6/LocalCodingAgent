"""Conservative, pure cheap-first routing policy. No inference or I/O occurs here."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from itertools import chain
from typing import Any, Iterable, Iterator, Mapping


@dataclass(frozen=True)
class RoutingDecision:
    tier: str
    reason: str
    source: str
    classifier_eligible: bool
    prompt_digest: str

    def describe(self) -> dict[str, str | bool]:
        """Return bounded metadata, never task text, filenames, or classifier output."""
        return asdict(self)


_PROTECTED_ROLES = {"security", "red_team", "bug_hunter", "reviewer", "exploit_validator"}
_PROTECTED_MODES = {"cybersecurity", "red_team", "bug_hunting"}
_RISK = re.compile(
    r"security|cyber|vulnerab|exploit|credential|password|secret|token|"
    r"auth|permission|privilege|crypt|encrypt|decrypt|injection|untrusted|"
    r"sandbox|traversal|deserializ|malware|red.team|bug.hunt|exfiltrat|"
    r"denial.of.service|brute.force|buffer.overflow|command.execution|"
    r"\b(?:xss|csrf|ssrf|rce|sql|tls|ssl|jwt|oauth|cve|acl|sqli|idor|chmod|chown)\b",
    re.I,
)
_COMPLEX = re.compile(
    r"\b(?:architect\w*|refactor\w*|migrat\w*|concurren\w*|race|deadlock|"
    r"performance|optimiz\w*|debug\w*|regression\w*|algorithm\w*|database|"
    r"deploy\w*|production|payment\w*|integrat\w*|implement\w*|"
    r"all files|entire|codebase|repository.wide|multiple|multi.file)\b", re.I,
)
_INSTRUCTION = re.compile(
    r"ignore|disregard|override|classifier|routing|system.prompt|previous.instructions|"
    r"\beconomy\b|\bselected\b|\bmodel\b", re.I,
)
_EDIT = re.compile(
    r"\b(?:add|change|create|delete|remove|rewrite|fix|update|edit|modify|"
    r"execute|run|install|build|write|replace|rename|format)\b", re.I,
)
_READ = re.compile(r"^(?:please\s+)?(?:summarize|explain|locate|find|show|list)\b", re.I)
_NARROW_READ = re.compile(
    r"^(?:please\s+)?(?:summarize|explain)\s+(?:the\s+)?"
    r"(?:contents?\s+of\s+)?[\w./`-]+\.(?:md|txt|rst|py|js|ts|tsx|jsx|go|rs)\.?$|"
    r"^(?:please\s+)?(?:locate|find)\s+(?:the\s+)?"
    r"(?:definition|file|function|class)\s+(?:of\s+|for\s+|named\s+)?[\w.`-]+\.?$", re.I,
)
_SMALL_DOC = re.compile(
    r"^(?:please\s+)?(?:fix|correct)\s+(?:a\s+|the\s+)?typo\s+in\s+"
    r"[\w./`-]+\.(?:md|txt|rst)\.?$", re.I,
)


def _values(value: Any) -> Iterator[str]:
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _values(item)
    else:
        yield str(value)


def _fingerprint(parts: Iterable[str]) -> tuple[str, str, int]:
    """Hash every source byte while retaining only the routing policy's allowance.

    Large source observations already exist in task memory; routing must not
    duplicate them into an unbounded joined string and UTF-8 byte allocation.
    Newline-separated digest semantics are retained for existing telemetry.
    """
    digest = hashlib.sha256()
    retained: list[str] = []
    length = 0
    for index, part in enumerate(parts):
        if index:
            digest.update(b"\n")
            if length < 2400:
                retained.append("\n")
            length += 1
        if length < 2400:
            retained.append(part[:2400 - length])
        length += len(part)
        for offset in range(0, len(part), 8192):
            digest.update(part[offset:offset + 8192].encode("utf-8"))
    return digest.hexdigest(), "".join(retained), length


def classify_task(
    task: Any, role: Any, mode: str = "coding", explicit: bool = False, retry: bool = False,
) -> RoutingDecision:
    """Choose economy only for recognizable narrow work; fail closed on ambiguity.

    The optional classifier is eligible only for unknown, short read-only requests.
    Protected decisions must never be passed through to a classifier downgrade.
    """
    def field(name: str, default: Any = None) -> Any:
        return task.get(name, default) if isinstance(task, Mapping) else getattr(task, name, default)

    objective = task if isinstance(task, str) else field("objective", "")
    objective = objective if isinstance(objective, str) else ""
    files = field("files", []) if not isinstance(task, str) else []
    context = field("context", {}) if not isinstance(task, str) else {}
    # Empty context keys (including security_lessons) are not task instructions.
    if hasattr(context, "model_dump"):
        context = context.model_dump(mode="json", exclude_defaults=True)
    digest, prompt, prompt_length = _fingerprint(chain(
        (objective,), _values(field("constraints", [])), _values(files),
        _values(context), _values(field("success_criteria", [])),
    ))

    def decision(reason: str, tier: str = "selected", eligible: bool = False) -> RoutingDecision:
        return RoutingDecision(tier, reason, "rules", eligible, digest)

    role_name = str(getattr(role, "value", role)).lower()
    mode_name = str(getattr(mode, "value", mode)).lower()
    if explicit or field("model_name"):
        return decision("explicit_model")
    if retry:
        return decision("retry")
    if role_name in _PROTECTED_ROLES or mode_name in _PROTECTED_MODES:
        return decision("protected_workflow")
    if mode_name != "coding" or role_name not in {"explorer", "coder"}:
        return decision("unsupported_workflow")
    if not objective.strip() or prompt_length > 2400 or len(objective) > 300:
        return decision("ambiguous_or_long")
    if _RISK.search(prompt):
        return decision("security_sensitive")
    if not prompt.isascii() or _INSTRUCTION.search(prompt):
        return decision("untrusted_or_unknown_language")
    if _COMPLEX.search(prompt) or len(files) > 1:
        return decision("complex_task")
    # Additional context is untrusted and can materially change a short objective.
    extra = prompt[len(objective) + 1:]
    if _EDIT.search(extra) or re.search(r"[;\n]", objective.strip()):
        return decision("additional_instructions")
    if role_name == "coder" and _SMALL_DOC.fullmatch(objective.strip()):
        return decision("small_documentation", "economy")
    if _EDIT.search(objective):
        return decision("editing_task")
    if _NARROW_READ.fullmatch(objective.strip()):
        return decision("narrow_read_only", "economy")
    if _READ.match(objective.strip()):
        return decision("unknown_read_only", eligible=True)
    return decision("uncertain_task")


def validate_classifier_reply(text: str) -> str | None:
    """Validate a small exact JSON reply; low confidence never permits economy."""
    if not isinstance(text, str) or len(text) > 256:
        return None
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate classifier key")
            result[key] = value
        return result
    try:
        reply = json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        return None
    if not isinstance(reply, dict) or set(reply) != {"tier", "confidence"}:
        return None
    tier, confidence = reply["tier"], reply["confidence"]
    if tier not in ("economy", "selected") or type(confidence) not in (int, float):
        return None
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return None
    if tier == "economy" and confidence < 0.90:
        return None
    return tier


def apply_classifier_decision(baseline: RoutingDecision, reply: str) -> RoutingDecision:
    """A classifier cannot weaken a protected rule, even if its reply says so."""
    if not baseline.classifier_eligible:
        return baseline
    tier = validate_classifier_reply(reply)
    return replace(
        baseline, tier=tier or "selected", source="local_classifier",
        classifier_eligible=False,
        reason=f"local_classifier_{tier}" if tier else "invalid_classifier_reply",
    )

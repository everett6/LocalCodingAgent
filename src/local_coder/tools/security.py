"""Security scanning of the local workspace.

Everything here analyzes files inside the project workspace only; nothing
connects to or probes other hosts. The built-in secret scanner needs no
dependencies. bandit and semgrep run when installed, and semgrep only with a
rule file checked into the project, so no rules are fetched over the network.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_coder.tools.base import Tool
from local_coder.tools.quality import EXCLUDED_DIRS, find_executable, run_bounded
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace

SCANNERS = ("secrets", "bandit", "semgrep")
SEMGREP_CONFIGS = (".semgrep.yml", ".semgrep.yaml", ".semgrep")
MAX_FILE_BYTES = 1_000_000
MAX_FINDINGS = 200
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    severity: str
    scanner: str
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line} [{self.severity}] {self.scanner}/{self.rule}: {self.message}"


SECRET_PATTERNS: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    ("private-key", "HIGH", "Private key block",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----")),
    ("aws-access-key", "HIGH", "AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", "HIGH", "GitHub token",
     re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("slack-token", "HIGH", "Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", "HIGH", "Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe-live-key", "HIGH", "Stripe live secret key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{24,}\b")),
    ("anthropic-api-key", "HIGH", "Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-api-key", "HIGH", "OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("hardcoded-credential", "MEDIUM", "Hard-coded credential",
     re.compile(
         # No leading \b, so prefixed names like DB_PASSWORD still match.
         r"""(?i)(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)\w*"""
         r"""["']?\s*[:=]\s*["']([^"'\s]{8,})["']"""
     )),
)
PLACEHOLDER_HINTS = ("example", "changeme", "change_me", "placeholder", "dummy", "your", "xxxx", "${", "<", "{{", "****")


def _redact(value: str) -> str:
    return f"{value[:4]}…({len(value)} chars)"


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in PLACEHOLDER_HINTS)


def _iter_files(root: Path, targets: list[Path]):
    for target in targets:
        for path in [target] if target.is_file() else sorted(target.rglob("*")):
            rel = path.relative_to(root)
            if any(part in EXCLUDED_DIRS for part in rel.parts) or not path.is_file():
                continue
            yield path, str(rel)


def scan_secrets(root: Path, targets: list[Path]) -> list[Finding]:
    """Find likely committed credentials; matched values are redacted."""
    findings: list[Finding] = []
    for path, rel in _iter_files(root, targets):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue  # binary
        text = data.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for rule, severity, label, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if match is None:
                    continue
                value = match.group(1) if match.groups() else match.group(0)
                if rule == "hardcoded-credential" and _is_placeholder(value):
                    continue
                shown = "" if rule == "private-key" else f" ({_redact(value)})"
                findings.append(Finding(rel, line_no, severity, "secrets", rule, f"{label}{shown}"))
                break  # one finding per line is enough
    return findings


def _normalize(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def parse_bandit(output: str) -> list[Finding]:
    data = json.loads(output or "{}")
    return [
        Finding(
            _normalize(item.get("filename", "?")),
            int(item.get("line_number", 0)),
            str(item.get("issue_severity", "LOW")).upper(),
            "bandit",
            item.get("test_id", "?"),
            f"{item.get('issue_text', '').strip()} (confidence {str(item.get('issue_confidence', '?')).lower()})",
        )
        for item in data.get("results", [])
    ]


def parse_semgrep(output: str) -> list[Finding]:
    data = json.loads(output or "{}")
    severity_map = {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW"}
    findings = []
    for item in data.get("results", []):
        extra = item.get("extra", {})
        severity = str(extra.get("severity", "INFO")).upper()
        findings.append(Finding(
            _normalize(item.get("path", "?")),
            int(item.get("start", {}).get("line", 0)),
            severity_map.get(severity, severity),
            "semgrep",
            item.get("check_id", "?"),
            str(extra.get("message", "")).strip().splitlines()[0] if extra.get("message") else "",
        ))
    return findings


class SecurityScanTool(Tool):
    name = ToolName.SECURITY_SCAN
    description = (
        "Scan project files for security issues: committed secrets (built in, values redacted), "
        "bandit for Python, and semgrep with the project's own rules. Reports path:line findings. "
        "Read-only; analyzes the local workspace only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional paths relative to project root"},
            "scanners": {
                "type": "array",
                "items": {"type": "string", "enum": list(SCANNERS)},
                "description": "Scanners to run (default: every available one)",
            },
        },
    }

    def __init__(self, project_root: str, timeout: int = 300):
        self.project_root = project_root
        self.timeout = timeout

    async def _run_json(self, argv: list[str], ok_codes: tuple[int, ...]) -> tuple[str | None, str]:
        """Run a scanner and return (stdout, error message)."""
        completed = await run_bounded(argv, self.project_root, self.timeout)
        if completed is None:
            return None, f"timed out after {self.timeout} seconds"
        returncode, stdout, stderr = completed
        if returncode not in ok_codes:
            return None, (stderr.decode(errors="replace").strip() or f"exit code {returncode}")[:2000]
        return stdout.decode(errors="replace"), ""

    def _semgrep_config(self) -> str | None:
        root = Path(self.project_root)
        return next((name for name in SEMGREP_CONFIGS if (root / name).exists()), None)

    async def execute(self, paths: list[str] | None = None, scanners: list[str] | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            workspace = Workspace(self.project_root)
            rel_paths = [workspace.relative_path(p) for p in (paths or ["."])]
            args = [p if not p.startswith("-") else f"./{p}" for p in rel_paths]
            explicit = scanners is not None
            requested = list(dict.fromkeys(scanners or SCANNERS))
            unknown = [s for s in requested if s not in SCANNERS]
            if unknown:
                return ToolResult(
                    success=False,
                    output=f"Unknown scanner(s): {', '.join(unknown)}. Choose from: {', '.join(SCANNERS)}",
                    duration_ms=int((time.time() - start_t) * 1000),
                )

            findings: list[Finding] = []
            ran: list[str] = []
            notes: list[str] = []
            errors: list[str] = []

            if "secrets" in requested:
                targets = [workspace.resolve(p) for p in rel_paths]
                findings += await asyncio.to_thread(scan_secrets, workspace.root, targets)
                ran.append("secrets")

            if "bandit" in requested:
                bandit = find_executable(self.project_root, "bandit")
                if bandit is None:
                    (errors if explicit else notes).append("bandit is not installed (pip install bandit)")
                else:
                    # bandit exits 1 when it reports issues; that is not a failure.
                    out, err = await self._run_json([bandit, "-r", "-f", "json", "-q", *args], (0, 1))
                    if out is None:
                        errors.append(f"bandit failed: {err}")
                    else:
                        findings += parse_bandit(out)
                        ran.append("bandit")

            if "semgrep" in requested:
                semgrep = find_executable(self.project_root, "semgrep")
                config = self._semgrep_config()
                if semgrep is None:
                    (errors if explicit else notes).append("semgrep is not installed")
                elif config is None:
                    (errors if explicit else notes).append(
                        f"semgrep skipped: no local rules ({', '.join(SEMGREP_CONFIGS)}); remote rule packs are not fetched"
                    )
                else:
                    out, err = await self._run_json(
                        [semgrep, "scan", "--config", config, "--json", "--metrics=off", "--quiet", *args], (0, 1),
                    )
                    if out is None:
                        errors.append(f"semgrep failed: {err}")
                    else:
                        findings += parse_semgrep(out)
                        ran.append("semgrep")

            findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.path, f.line))
            counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in SEVERITY_ORDER}
            summary = ", ".join(f"{n} {sev.lower()}" for sev, n in counts.items() if n) or "none"
            lines = [f"Scanners run: {', '.join(ran) or 'none'}", f"Findings: {len(findings)} ({summary})"]
            lines += [f.render() for f in findings[:MAX_FINDINGS]]
            if len(findings) > MAX_FINDINGS:
                lines.append(f"...[{len(findings) - MAX_FINDINGS} more findings not shown; narrow paths to see them]")
            lines += [f"Note: {n}" for n in notes]
            lines += [f"Error: {e}" for e in errors]
            return ToolResult(
                success=not errors and bool(ran),
                output="\n".join(lines),
                duration_ms=int((time.time() - start_t) * 1000),
            )
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time() - start_t) * 1000))

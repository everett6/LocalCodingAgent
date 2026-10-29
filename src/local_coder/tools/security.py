"""Security testing and audit tools for the coding agent."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import stat
import time
from itertools import islice
from pathlib import Path
from typing import Any

from local_coder.workspace import Workspace
from local_coder.context.source_snapshot import read_source_snapshot
from local_coder.processes import OUTPUT_TRUNCATION_MARKER, communicate as _communicate, process_options
from local_coder.tools.scan_scope import read_scan_source, record_scan_skip, scan_files
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult
from dataclasses import dataclass
from local_coder.security.ledger import SEVERITIES, STATUSES, FindingsLedger, LedgerEntry
from local_coder.security.lessons import LESSONS_FILE, LessonStore
from local_coder.security.triage import (
    BASELINE_FILE, LAST_SCAN_FILE, load_fingerprints, save_fingerprints, triage,
)
from local_coder.tools.quality import EXCLUDED_DIRS, find_executable, run_bounded


# Structured scanner JSON is not an ordinary human-readable log tail. Retain
# complete bounded streams and archive the original bytes before parsing them.
MAX_SCANNER_STREAM_BYTES = 16 * 1024 * 1024
MAX_SAST_RESULT_BYTES = 4_000_000
MAX_SAST_FINDINGS = 2000
MAX_SAST_OBSERVATIONS = 4000


def _retain_sast_observation(coverage: dict, value: Any, *, finding: bool = False) -> bool:
    size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if (coverage.get("result_bytes", 0) + size > MAX_SAST_RESULT_BYTES
            or coverage.get("result_records", 0) >= MAX_SAST_OBSERVATIONS
            or (finding and coverage.get("finding_records", 0) >= MAX_SAST_FINDINGS)):
        record_scan_skip(coverage, ".", "Static result budget reached; narrow the target to inspect remaining evidence")
        coverage["result_limit_reached"] = True
        return False
    coverage["result_bytes"] = coverage.get("result_bytes", 0) + size
    coverage["result_records"] = coverage.get("result_records", 0) + 1
    if finding:
        coverage["finding_records"] = coverage.get("finding_records", 0) + 1
    return True


@dataclass(frozen=True)
class _ScannerCapture:
    returncode: int | None
    stdout: str
    parsed: dict | None
    archive_reference: str
    error: str = ""


async def _capture_scanner(project_root: str | Path, engine: str, argv: list[str],
                           timeout: int, ok_codes: tuple[int, ...] = (0, 1)) -> _ScannerCapture:
    from local_coder.security.scan_archive import ScanArchive
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(project_root), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **process_options(),
    )
    timed_out = False
    try:
        stdout, stderr = await _communicate(proc, timeout=timeout,
            max_stream_bytes=MAX_SCANNER_STREAM_BYTES, max_total_bytes=MAX_SCANNER_STREAM_BYTES * 2)
        stdout, stderr = stdout or b"", stderr or b""
    except asyncio.TimeoutError:
        stdout, stderr, timed_out = b"", b"", True
    truncated = any(stream.startswith(OUTPUT_TRUNCATION_MARKER) for stream in (stdout, stderr))
    disposition = "timeout" if timed_out else "over_limit" if truncated else "complete"
    reference = await asyncio.to_thread(ScanArchive(project_root).save_capture, engine,
        {"argv": argv, "timeout_seconds": timeout}, stdout, stderr,
        returncode=proc.returncode, stream_limit=MAX_SCANNER_STREAM_BYTES,
        complete=not timed_out and not truncated, disposition=disposition)
    if timed_out:
        error = f"{engine} timed out; original scanner output is unavailable and coverage is unknown"
    elif truncated:
        error = (f"{engine} exceeded the scanner capture limit ({MAX_SCANNER_STREAM_BYTES} bytes per stream); "
                 "incomplete JSON was not parsed; narrow the scan")
    elif proc.returncode not in ok_codes:
        error = stderr.decode("utf-8", errors="replace").strip()[:2000] or f"{engine} exit code {proc.returncode}"
    else:
        try:
            text = stdout.decode("utf-8")
            parsed = json.loads(text)
            if (not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list)
                    or any(not isinstance(item, dict) for item in parsed["results"])
                    or not isinstance(parsed.get("errors", []), list)):
                raise ValueError("Expected scanner object with results and optional errors lists")
            return _ScannerCapture(proc.returncode, text, parsed, reference)
        except (ValueError, UnicodeError, RecursionError):
            error = f"{engine} returned invalid structured JSON; scan coverage is unknown"
    return _ScannerCapture(proc.returncode, "", None, reference, error)


def _capture_failure(engine: str, coverage: dict, capture: _ScannerCapture) -> ToolResult:
    coverage["status"] = "failed"
    coverage.setdefault("failed", []).append({"scanner": engine, "reason": capture.error})
    return ToolResult(success=False, error=capture.error, exit_code=capture.returncode,
                      data={"coverage": coverage, "scanner_capture_archives": {engine: capture.archive_reference}})


class RunSASTTool(Tool):
    """Run Static Application Security Testing (SAST) on the codebase."""

    name = ToolName.RUN_SAST
    description = (
        "Run Static Application Security Testing (SAST) to detect security vulnerabilities, "
        "dangerous sinks (eval, raw SQL, shell execution), and insecure coding patterns."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target_path": {
                "type": "string",
                "description": "Optional relative path to file or directory to scan. Defaults to project root.",
            },
            "tool": {
                "type": "string",
                "enum": ["auto", "semgrep", "bandit", "builtin"],
                "description": "SAST engine to use: 'auto' (prefers semgrep/bandit if installed, falls back to builtin), 'semgrep', 'bandit', or 'builtin'.",
            },
        },
    }

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    async def execute(self, target_path: str = ".", tool: str = "auto", **kwargs: Any) -> ToolResult:
        from local_coder.security.scan_archive import ScanArchive
        result = await self._execute(target_path, tool, **kwargs)
        try:
            reference = await asyncio.to_thread(ScanArchive(self.project_root).save,
                                                self.name.value, {"target_path": target_path, "tool": tool}, result)
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, error=f"Full scan archive failed: {exc}",
                              data={"coverage": {"status": "unknown", "failed": ["archive unavailable"]},
                                    "scanner_capture_archives": result.data.get("scanner_capture_archives", {})})
        result.data["archive_reference"] = reference
        result.output += f"\nFull scan archive: {reference}"
        return result

    async def _execute(
        self,
        target_path: str = ".",
        tool: str = "auto",
        **kwargs: Any,
    ) -> ToolResult:
        start_t = time.time()
        try:
            scan_target = Workspace(self.project_root).resolve(target_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        files, coverage = scan_files(self.project_root, scan_target)
        if not scan_target.exists():
            return ToolResult(
                success=False,
                error=f"Scan target does not exist: {target_path}",
                duration_ms=(time.time() - start_t) * 1000,
            )

        try:
            # 1. Attempt semgrep if available and requested
            if tool == "semgrep" and shutil.which("semgrep"):
                result = await self._run_semgrep(scan_target, files, coverage)
                if result is not None:
                    result.duration_ms = (time.time() - start_t) * 1000
                    return result

            # 2. Attempt bandit for Python files if available and requested
            if tool == "bandit" and shutil.which("bandit"):
                result = await self._run_bandit(scan_target, files, coverage)
                if result is not None:
                    result.duration_ms = (time.time() - start_t) * 1000
                    return result

            if tool not in ("auto", "builtin"):
                return ToolResult(success=False, error=f"Requested scanner {tool} is unavailable or failed; scan was not checked")
            # 3. Fallback to builtin AST / pattern security analyzer
            findings = await asyncio.to_thread(self._run_builtin_sast, scan_target, files, coverage)
            duration_ms = (time.time() - start_t) * 1000

            summary = {
                "engine": "builtin-sast",
                "scanned_path": str(scan_target.relative_to(self.project_root)),
                "total_findings": len(findings),
                "findings": [dict({"confidence": "heuristic", "verification": "unverified"}, **f) for f in findings],
                "coverage": coverage,
                "local_analysis": coverage.pop("local_analysis", {}),
            }

            output_text = self._format_findings_markdown(summary)
            output_text += "\nLocal semantic surface (unverified): " + json.dumps(summary["local_analysis"], ensure_ascii=False)
            output_text += "\nInput coverage: " + json.dumps(coverage, ensure_ascii=False)
            return ToolResult(
                success=True,
                output=output_text + f"\nCoverage: {coverage['status']}. Static candidates require manual verification.",
                data=summary,
                duration_ms=duration_ms,
            )

        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"SAST scan failed: {exc}",
                duration_ms=(time.time() - start_t) * 1000,
            )

    async def _run_semgrep(self, scan_target: Path, files, coverage) -> ToolResult | None:
        rules = Workspace(self.project_root).resolve(".semgrep.yml")
        if not rules.is_file():
            return None
        cmd = ["semgrep", "scan", "--config", str(rules), "--json", "--quiet", *map(str, files)]
        capture = await _capture_scanner(self.project_root, "semgrep", cmd, 60)
        if capture.error:
            return _capture_failure("semgrep", coverage, capture)
        if capture.returncode in (0, 1):
            try:
                data = capture.parsed
                if data.get("errors"):
                    coverage["skipped"].extend(str(error.get("type", "Scanner error")) for error in data["errors"])
                    coverage["status"] = "partial"
                findings = []
                for res in data.get("results", []):
                    findings.append({
                        "file": res.get("path"),
                        "line": res.get("start", {}).get("line"),
                        "severity": res.get("extra", {}).get("severity", "WARNING"),
                        "rule_id": res.get("check_id"),
                        "message": res.get("extra", {}).get("message"),
                        "code": res.get("extra", {}).get("lines", ""),
                    })
                summary = {
                    "engine": "semgrep",
                    "scanned_path": str(scan_target.relative_to(self.project_root)),
                    "total_findings": len(findings),
                    "findings": findings,
                }
                return ToolResult(
                    success=True,
                    output=self._format_findings_markdown(summary) + f"\nCoverage: {coverage['status']}",
                    data={**summary, "coverage": coverage, "raw_scan": data,
                          "scanner_capture_archives": {"semgrep": capture.archive_reference}},
                    exit_code=capture.returncode,
                )
            except (ValueError, TypeError, KeyError, AttributeError):
                return _capture_failure("semgrep", coverage, _ScannerCapture(capture.returncode, "", None,
                    capture.archive_reference, "semgrep results could not be interpreted; coverage is unknown"))
        return None

    async def _run_bandit(self, scan_target: Path, files, coverage) -> ToolResult | None:
        cmd = ["bandit", "-f", "json", "-q", *map(str, files)]
        capture = await _capture_scanner(self.project_root, "bandit", cmd, 60)
        if capture.error:
            return _capture_failure("bandit", coverage, capture)
        try:
            data = capture.parsed
            if data.get("errors"):
                coverage["skipped"].extend("Bandit could not parse a file" for _ in data["errors"])
                coverage["status"] = "partial"
            findings = []
            for res in data.get("results", []):
                findings.append({
                    "file": res.get("filename"),
                    "line": res.get("line_number"),
                    "severity": res.get("issue_severity", "MEDIUM"),
                    "rule_id": res.get("test_id"),
                    "message": res.get("issue_text"),
                    "code": res.get("code", "").strip(),
                })
            summary = {
                "engine": "bandit",
                "scanned_path": str(scan_target.relative_to(self.project_root)),
                "total_findings": len(findings),
                "findings": findings,
            }
            return ToolResult(
                success=True,
                output=self._format_findings_markdown(summary) + f"\nCoverage: {coverage['status']}",
                data={**summary, "coverage": coverage, "raw_scan": data,
                      "scanner_capture_archives": {"bandit": capture.archive_reference}},
                exit_code=capture.returncode,
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            return _capture_failure("bandit", coverage, _ScannerCapture(capture.returncode, "", None,
                capture.archive_reference, "bandit results could not be interpreted; coverage is unknown"))

    def _run_builtin_sast(self, scan_target: Path, files, coverage) -> list[dict[str, Any]]:
        """Builtin AST-based and pattern security scanner for Python and JS/TS."""
        findings: list[dict[str, Any]] = []
        coverage.update(reviewed=[], failed=[], unknown=[])
        coverage["result_limits"] = {"bytes": MAX_SAST_RESULT_BYTES, "findings": MAX_SAST_FINDINGS,
                                     "records": MAX_SAST_OBSERVATIONS}
        coverage["local_analysis"] = {"entry_points": [], "authorization_traces": [],
                                      "vulnerability_classes": [], "gaps": []}
        for file_path in files:
            relative = file_path.relative_to(self.project_root).as_posix()
            if file_path.suffix == ".py":
                findings.extend(self._scan_python_file(file_path, coverage))
            else:
                if file_path.suffix in (".js", ".jsx", ".ts", ".tsx"):
                    for finding in self._scan_js_file(file_path, coverage):
                        if not _retain_sast_observation(coverage, finding, finding=True):
                            break
                        findings.append(finding)
                coverage["unknown"].append({"path": relative,
                    "reason": "Semantic tracing supports Python only; other languages and formats require review"})
                coverage["status"] = "partial"
            if coverage.get("result_limit_reached") or len(findings) >= MAX_SAST_FINDINGS:
                record_scan_skip(coverage, ".", "Static result limit reached; remaining files were not analyzed")
                coverage["result_limit_reached"] = True
                break
        if not coverage["reviewed"]:
            coverage["status"] = "partial"
        return findings

    def _is_ignored(self, path: Path) -> bool:
        ignore_parts = {".git", ".venv", "venv", "node_modules", "__pycache__", ".local-coder", "build", "dist"}
        return any(part in ignore_parts for part in path.parts)

    def _scan_python_file(self, file_path: Path, coverage) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        try:
            rel_path = str(file_path.relative_to(self.project_root))
            content = read_scan_source(self.project_root, file_path, coverage=coverage, errors="replace")
            ast.parse(content, filename=str(file_path))
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as exc:
            coverage.setdefault("failed", []).append({"path": str(file_path.relative_to(self.project_root)), "reason": type(exc).__name__})
            record_scan_skip(coverage, file_path, type(exc).__name__)
            return findings

        from local_coder.tools.detection import python_candidates
        from local_coder.security.local_analysis import analyze_unit
        from local_coder.security_analysis import AnalysisLimit
        try:
            analysis = analyze_unit(content, rel_path)
            findings = python_candidates(content, rel_path)
        except (AnalysisLimit, RecursionError) as exc:
            coverage.setdefault("failed", []).append({"path": rel_path, "reason": type(exc).__name__})
            coverage["skipped"].append(f"{rel_path}: {type(exc).__name__}")
            coverage["status"] = "partial"
            return []
        coverage.setdefault("reviewed", []).append(rel_path)
        aggregated = coverage.setdefault("local_analysis", {"entry_points": [], "authorization_traces": [],
                                                           "vulnerability_classes": [], "gaps": []})
        for key in aggregated:
            for observation in analysis[key]:
                if observation not in aggregated[key]:
                    if not _retain_sast_observation(coverage, observation):
                        return []
                    aggregated[key].append(observation)
        retained = []
        for finding in findings:
            function = analysis["functions"].get(finding.get("function"), {})
            finding["semantic_trace"] = {"language": "python", "sources": finding.get("source_trace", []),
                "transforms": [item for item in function.get("transforms", []) if item["line"] < finding["line"]],
                "guards": function.get("guards", []), "sink": finding["sink"],
                "gaps": analysis["gaps"], "verification": "unverified"}
            if not _retain_sast_observation(coverage, finding, finding=True):
                break
            retained.append(finding)
        return retained

    def _scan_js_file(self, file_path: Path, coverage: dict | None = None) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        try:
            rel_path = str(file_path.relative_to(self.project_root))
            content = read_scan_source(self.project_root, file_path, coverage=coverage, errors="replace")
        except (OSError, UnicodeError, ValueError) as exc:
            if coverage is not None:
                record_scan_skip(coverage, file_path, type(exc).__name__)
            return findings

        in_block_comment = False
        for i, line in enumerate(content.splitlines(), start=1):
            if len(findings) >= MAX_SAST_FINDINGS:
                if coverage is not None:
                    record_scan_skip(coverage, rel_path, "JavaScript finding limit reached; narrow the target")
                    coverage["result_limit_reached"] = True
                break
            # A lexical fallback, not a JavaScript parser. Ignore standalone
            # comments and report the remaining syntax limitations explicitly.
            stripped = line.strip()
            if in_block_comment:
                in_block_comment = "*/" not in stripped
                continue
            if stripped.startswith("/*"):
                in_block_comment = "*/" not in stripped
                continue
            if stripped.startswith("//"):
                continue
            if re.search(r"\beval\s*\(", line):
                findings.append({
                    "file": rel_path,
                    "line": i,
                    "severity": "CRITICAL",
                    "rule_id": "SEC-JS-EVAL",
                    "message": "Use of eval() introduces arbitrary script execution risks",
                    "code": line.strip(),
                })
            elif re.search(r"\.innerHTML\s*=", line) and not re.search(r"=\s*(?:DOMPurify\.sanitize\(|[\"\'])", line):
                findings.append({
                    "file": rel_path,
                    "line": i,
                    "severity": "MEDIUM",
                    "rule_id": "SEC-JS-INNERHTML-XSS",
                    "message": "Assignment to innerHTML may lead to Cross-Site Scripting (XSS); use textContent or sanitize",
                    "code": line.strip(),
                })
            elif re.search(r"child_process\.exec\s*\(", line):
                findings.append({
                    "file": rel_path,
                    "line": i,
                    "severity": "HIGH",
                    "rule_id": "SEC-JS-COMMAND-EXEC",
                    "message": "child_process.exec() executes within a shell; prefer child_process.execFile() with args array",
                    "code": line.strip(),
                })

        for finding in findings:
            finding.update({"verification": "unverified", "confidence": "heuristic",
                            "sink": {"line": finding["line"], "code": finding["code"]},
                            "source_trace": [], "guards": [],
                            "preconditions": ["Attacker-controlled input reaches the sink", "Relevant guards do not enforce safety"],
                            "coverage_limits": ["lexical JavaScript scan; no parser/dataflow", "caller and guards require manual inspection", "no code executed"]})
        return findings

    def _format_findings_markdown(self, summary: dict[str, Any]) -> str:
        findings = summary.get("findings", [])
        if not findings:
            return f"### SAST Scan: Static review\nEngine: `{summary.get('engine')}`\nTarget: `{summary.get('scanned_path')}`\nNo matching static candidates detected; runtime exploitability was not checked. Empty output does not prove safety."

        md = [
            f"### SAST Security Report ({len(findings)} findings)",
            f"- **Engine**: `{summary.get('engine')}`",
            f"- **Target**: `{summary.get('scanned_path')}`",
            "",
            "| Severity | Rule ID | Location | Issue |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for f in findings:
            loc = f"`{f.get('file')}:{f.get('line')}`"
            sev = f"**{f.get('severity')}**"
            rule = f"`{f.get('rule_id')}`"
            msg = f.get("message", "").replace("|", "\\|")
            md.append(f"| {sev} | {rule} | {loc} | {msg} |")

        md.append("\n#### Finding Details:")
        for idx, f in enumerate(findings[:15], start=1):
            md.append(f"\n**{idx}. [{f.get('severity')}] {f.get('rule_id')}** in `{f.get('file')}:{f.get('line')}`")
            md.append(f"> {f.get('message')}")
            if f.get("sink"):
                evidence = {key: f[key] for key in ("source_trace", "semantic_trace", "sink", "function", "callers", "guards", "decorators", "preconditions", "coverage_limits") if key in f}
                md.append("Static evidence (untrusted; reachability and guard efficacy require review):")
                md.append("```json\n" + json.dumps(evidence, ensure_ascii=False) + "\n```")
            if f.get("code"):
                md.append(f"```\n{f.get('code')}\n```")

        if len(findings) > 15:
            md.append(f"\n*(Showing top 15 of {len(findings)} findings)*")

        return "\n".join(md)


class AuditDependenciesTool(Tool):
    """Audit only the project's manifests; unavailable coverage is explicit."""
    name = ToolName.AUDIT_DEPENDENCIES
    description = "Audit project dependency manifests. Reports unavailable advisory coverage separately from actual advisories."
    parameters = {"type": "object", "properties": {"manifest_type": {"enum": ["auto", "python", "node"], "type": "string"}}}

    def __init__(self, project_root):
        self.project_root = Path(project_root).resolve()

    async def execute(self, manifest_type="auto", **kwargs):
        workspace = Workspace(self.project_root)
        findings, inventory, coverage = [], [], []
        for ecosystem in (["python", "node"] if manifest_type == "auto" else [manifest_type]):
            try:
                if ecosystem == "python":
                    import tomllib
                    requirements = workspace.resolve("requirements.txt")
                    pyproject = workspace.resolve("pyproject.toml")
                    if requirements.is_file():
                        inventory.extend(requirements.read_text(encoding="utf-8").splitlines()[:200])
                    elif pyproject.is_file():
                        inventory.extend(tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("dependencies", [])[:200])
                    executable = shutil.which("pip-audit")
                    if not executable or not requirements.is_file():
                        coverage.append("Python advisories not checked: pip-audit and a pinned requirements.txt are required")
                        continue
                    cmd = [executable, "--format", "json", "--disable-pip", "--no-deps", "-r", str(requirements)]
                elif ecosystem == "node":
                    manifest = workspace.resolve("package.json")
                    if not manifest.is_file():
                        coverage.append("Node advisories not checked: no package.json in selected workspace")
                        continue
                    inventory.extend(json.loads(manifest.read_text(encoding="utf-8")).get("dependencies", {}).keys())
                    executable = shutil.which("npm")
                    if not executable or not workspace.resolve("package-lock.json").is_file():
                        coverage.append("Node advisories not checked: npm and package-lock.json are required")
                        continue
                    cmd = [executable, "audit", "--json", "--ignore-scripts"]
                else:
                    raise ValueError("Unknown dependency ecosystem")
                proc = await asyncio.create_subprocess_exec(*cmd, cwd=self.project_root,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **process_options())
                stdout, _ = await _communicate(proc, timeout=60)
                data = json.loads(stdout)
                if proc.returncode not in (0, 1) or data.get("error"):
                    coverage.append(f"{ecosystem} advisories not checked: scanner failed")
                    continue
                if ecosystem == "python":
                    if "dependencies" not in data:
                        raise ValueError("Missing dependency results")
                    for dep in data["dependencies"]:
                        for vuln in dep.get("vulns", []):
                            findings.append({"package": dep["name"], "version": dep["version"],
                                             "id": vuln["id"], "fix_versions": vuln.get("fix_versions", [])})
                else:
                    if "vulnerabilities" not in data:
                        raise ValueError("Missing vulnerability results")
                    findings.extend(data["vulnerabilities"].values())
                coverage.append(f"{ecosystem} advisory scanner completed")
            except (OSError, ValueError, asyncio.TimeoutError):
                coverage.append(f"{ecosystem} advisories not checked: manifest invalid, outside workspace, or scanner timed out")
        text = "### Dependency Audit\n" + "\n".join(coverage)
        text += "\nDeclared dependencies: " + (", ".join(inventory) or "none discovered")
        text += "\nAdvisories: " + json.dumps(findings)
        return ToolResult(success=True, output=text, data={"findings": findings, "coverage": coverage})


class AuditConfigTool(Tool):
    """Audit repository configuration files for secrets and insecure defaults."""

    name = ToolName.AUDIT_CONFIG
    description = (
        "Scan configuration files (.env, Dockerfiles, Kubernetes manifests, CI/CD YAML) "
        "for leaked secrets, hardcoded API keys, and insecure configuration defaults."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Optional subdirectory or file to audit. Defaults to project root.",
            },
        },
    }

    SECRET_PATTERNS = [
        (r"(?i)(api[_-]?key|secret[_-]?key|auth[_-]?token|password)\s*[:=]\s*['\"]([a-zA-Z0-9_\-]{16,})['\"]", "High-entropy secret/credential token"),
        (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "Exposed private key certificate block"),
        (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
        (r"ghp_[a-zA-Z0-9]{36}", "GitHub Personal Access Token"),
        (r"xox[baprs]-[0-9a-zA-Z\-]{10,}", "Slack Token"),
    ]

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    async def execute(self, path: str = ".", **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            target = Workspace(self.project_root).resolve(path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        if not target.exists():
            return ToolResult(
                success=False,
                error=f"Target path does not exist: {path}",
                duration_ms=(time.time() - start_t) * 1000,
            )

        findings, coverage = await asyncio.to_thread(self._scan_configs, target)
        duration_ms = (time.time() - start_t) * 1000

        if not findings:
            return ToolResult(
                success=True,
                data={"findings": findings, "coverage": coverage},
                output=f"### Config & Secret Audit\nCoverage: {coverage['status']}. Audited `{path}`. No matching secret/config patterns found. This is a heuristic scan, not proof that secrets are absent.",
                duration_ms=duration_ms,
            )

        md = [
            f"### Config & Secret Audit ({len(findings)} issues found)",
            "",
            "| Severity | File | Line | Category | Description |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for f in findings:
            sev = f"**{f.get('severity')}**"
            loc = f"`{f.get('file')}`"
            line = f.get("line")
            cat = f.get("category")
            desc = f.get("description", "").replace("|", "\\|")
            md.append(f"| {sev} | {loc} | {line} | {cat} | {desc} |")

        md.append("\n#### Remediation Advice:")
        for idx, f in enumerate(findings[:10], start=1):
            md.append(f"\n{idx}. **{f.get('file')}:{f.get('line')}** [{f.get('severity')}]")
            md.append(f"   - **Issue**: {f.get('description')}")
            md.append(f"   - **Remediation**: {f.get('remediation')}")

        return ToolResult(
            success=True,
            output="\n".join(md) + f"\nCoverage: {coverage['status']}",
            data={"findings": findings, "coverage": coverage},
            duration_ms=duration_ms,
        )

    def _scan_configs(self, target: Path) -> tuple[list[dict[str, Any]], dict]:
        findings: list[dict[str, Any]] = []
        files, coverage = scan_files(self.project_root, target)

        for file_path in files:
            name = file_path.name.lower()
            rel_path = str(file_path.relative_to(self.project_root))
            secret_text = file_path.suffix in (".py", ".js", ".ts", ".env", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf") or name.startswith(".env")
            dockerfile = "dockerfile" in name
            settings = name.startswith(".env") or file_path.suffix in (".yaml", ".yml", ".json")
            if not (secret_text or dockerfile or settings):
                continue
            try:
                content = read_scan_source(self.project_root, file_path, coverage=coverage, errors="replace")
            except (OSError, UnicodeError, ValueError) as exc:
                record_scan_skip(coverage, file_path, type(exc).__name__)
                continue

            # 1. Secret Scanning across text files
            if secret_text:
                findings.extend(self._scan_for_secrets(file_path, rel_path, content=content))

            # 2. Dockerfile security checks
            if dockerfile:
                findings.extend(self._scan_dockerfile(file_path, rel_path, content=content))

            # 3. Environment & config settings
            if settings:
                findings.extend(self._scan_config_settings(file_path, rel_path, content=content))

        return findings, coverage

    def _is_ignored(self, path: Path) -> bool:
        ignore_parts = {".git", ".venv", "venv", "node_modules", "__pycache__", ".local-coder", "build", "dist"}
        return any(part in ignore_parts for part in path.parts)

    def _scan_for_secrets(self, file_path: Path, rel_path: str, *, content: str | None = None) -> list[dict[str, Any]]:
        findings = []
        try:
            if content is None:
                content = read_scan_source(self.project_root, file_path, errors="replace")
        except Exception:
            return findings

        # Skip example or test fixtures unless explicitly requested
        if "test" in rel_path.lower() or "example" in rel_path.lower():
            return findings

        for i, line in enumerate(content.splitlines(), start=1):
            for pattern, desc in self.SECRET_PATTERNS:
                if re.search(pattern, line):
                    # Mask the line for safety
                    findings.append({
                        "file": rel_path,
                        "line": i,
                        "severity": "CRITICAL",
                        "category": "Hardcoded Secret",
                        "description": desc,
                        "remediation": "Move sensitive credentials out of source code into environment variables or secrets manager.",
                    })
                    break
        return findings

    def _scan_dockerfile(self, file_path: Path, rel_path: str, *, content: str | None = None) -> list[dict[str, Any]]:
        findings = []
        try:
            if content is None:
                content = read_scan_source(self.project_root, file_path, errors="replace")
        except Exception:
            return findings

        lines = content.splitlines()
        has_user_directive = False

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("USER ") and not stripped.endswith("root"):
                has_user_directive = True

            if stripped.startswith("FROM ") and ":latest" in stripped:
                findings.append({
                    "file": rel_path,
                    "line": i,
                    "severity": "LOW",
                    "category": "Container Hardening",
                    "description": "Base image uses mutable ':latest' tag",
                    "remediation": "Pin base image to a specific immutable tag or digest SHA.",
                })

            if stripped.startswith("ADD ") and not stripped.endswith(".tar.gz"):
                findings.append({
                    "file": rel_path,
                    "line": i,
                    "severity": "LOW",
                    "category": "Container Hardening",
                    "description": "Use COPY instead of ADD for local files",
                    "remediation": "Replace ADD with COPY unless auto-unpacking tar archives or fetching remote URLs.",
                })

        if not has_user_directive:
            findings.append({
                "file": rel_path,
                "line": len(lines) if lines else 1,
                "severity": "MEDIUM",
                "category": "Least Privilege",
                "description": "Dockerfile does not declare a non-root USER directive",
                "remediation": "Add 'USER <non-root-user>' to avoid running container processes with root privileges.",
            })

        return findings

    def _scan_config_settings(self, file_path: Path, rel_path: str, *, content: str | None = None) -> list[dict[str, Any]]:
        findings = []
        try:
            if content is None:
                content = read_scan_source(self.project_root, file_path, errors="replace")
        except Exception:
            return findings

        for i, line in enumerate(content.splitlines(), start=1):
            if re.search(r"(?i)\bDEBUG\s*=\s*(true|1)\b", line):
                findings.append({
                    "file": rel_path,
                    "line": i,
                    "severity": "HIGH",
                    "category": "Insecure Setting",
                    "description": "Debug mode explicitly enabled in configuration",
                    "remediation": "Ensure DEBUG is False in production environments to avoid leaking stack traces.",
                })
            if re.search(r"(?i)(allow_origins|cors_origin)\s*[:=]\s*['\"]?\*['\"]?", line):
                findings.append({
                    "file": rel_path,
                    "line": i,
                    "severity": "MEDIUM",
                    "category": "Permissive CORS",
                    "description": "Wildcard CORS origin ('*') enabled",
                    "remediation": "Restrict CORS allowed origins to trusted domains.",
                })
        return findings


SCANNERS = ("secrets", "bandit", "semgrep")
SEMGREP_CONFIGS = (".semgrep.yml", ".semgrep.yaml", ".semgrep")
MAX_FILE_BYTES = 1_000_000
MAX_SECRET_SCAN_ENTRIES = 20_000
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
    seen, enumerated = set(), 0
    for target in targets:
        if target in seen:
            continue
        enumerated += 1
        if enumerated > MAX_SECRET_SCAN_ENTRIES:
            raise ValueError("Secret scan enumeration limit reached; narrow the target paths")
        pending = [target]
        while pending:
            path = pending.pop()
            rel = path.relative_to(root)
            if any(part in EXCLUDED_DIRS for part in rel.parts) or path in seen:
                continue
            seen.add(path)
            try:
                info = path.lstat()
            except OSError:
                continue
            # Prune before descending, rather than materializing an entire
            # dependency tree and excluding its paths only afterward.
            if stat.S_ISDIR(info.st_mode):
                try:
                    with os.scandir(path) as entries:
                        children = list(islice(entries, MAX_SECRET_SCAN_ENTRIES - enumerated + 1))
                except OSError:
                    continue
                if len(children) > MAX_SECRET_SCAN_ENTRIES - enumerated:
                    raise ValueError("Secret scan enumeration limit reached; narrow the target paths")
                enumerated += len(children)
                pending.extend(Path(entry.path) for entry in sorted(children, key=lambda entry: entry.name, reverse=True))
            elif stat.S_ISREG(info.st_mode):
                yield path, str(rel)


def scan_secrets(root: Path, targets: list[Path]) -> list[Finding]:
    """Find likely committed credentials; matched values are redacted."""
    findings: list[Finding] = []
    for path, rel in _iter_files(root, targets):
        try:
            data = read_source_snapshot(root, path, max_bytes=MAX_FILE_BYTES).data
        except (OSError, ValueError):
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

    async def _run_json(self, argv: list[str], ok_codes: tuple[int, ...],
                        captures: dict[str, str] | None = None, *,
                        engine: str | None = None) -> tuple[str | None, str]:
        """Retain the two-value caller contract while archiving raw process bytes."""
        # The caller knows the scanner even when its executable has a project-local name.
        label = engine or Path(argv[0]).name
        capture = await _capture_scanner(self.project_root, label, argv, self.timeout, ok_codes)
        if captures is not None:
            captures[label] = capture.archive_reference
        if capture.error:
            return None, capture.error
        errors = (capture.parsed or {}).get('errors', [])
        return capture.stdout, (f'{label} reported {len(errors)} scanner errors; coverage is incomplete; '
                                f'original evidence: {capture.archive_reference}' if errors else '')

    @staticmethod
    def _remember_scan(root: Path, fingerprints: set[str]) -> None:
        """Accumulate what this review's scans saw; the coordinator turns it
        into the baseline when the review finishes."""
        try:
            previous = load_fingerprints(root / LAST_SCAN_FILE) or set()
            save_fingerprints(root / LAST_SCAN_FILE, previous | fingerprints)
        except OSError:
            pass

    def _semgrep_config(self) -> str | None:
        root = Path(self.project_root)
        return next((name for name in SEMGREP_CONFIGS if (root / name).exists()), None)

    async def execute(self, paths: list[str] | None = None, scanners: list[str] | None = None, **kwargs: Any) -> ToolResult:
        from local_coder.security.scan_archive import ScanArchive
        result = await self._execute(paths, scanners, **kwargs)
        try:
            reference = await asyncio.to_thread(ScanArchive(self.project_root).save, self.name.value,
                                                {"paths": paths, "scanners": scanners}, result)
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, error=f"Full baseline scan archive failed: {exc}",
                              data={"coverage": {"status": "unknown", "failed": ["archive unavailable"]},
                                    "scanner_capture_archives": result.data.get("scanner_capture_archives", {})})
        result.data["archive_reference"] = reference
        result.output += f"\nFull baseline scan archive: {reference}"
        return result

    async def _execute(self, paths: list[str] | None = None, scanners: list[str] | None = None, **kwargs: Any) -> ToolResult:
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
            scanner_outputs: dict[str, str] = {}
            scanner_capture_archives: dict[str, str] = {}

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
                    out, err = await self._run_json([bandit, "-r", "-f", "json", "-q", *args], (0, 1),
                                                    scanner_capture_archives, engine="bandit")
                    if out is None:
                        errors.append(f"bandit failed: {err}")
                    else:
                        scanner_outputs["bandit"] = out
                        if err:
                            errors.append(err)
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
                        scanner_capture_archives, engine="semgrep",
                    )
                    if out is None:
                        errors.append(f"semgrep failed: {err}")
                    else:
                        scanner_outputs["semgrep"] = out
                        if err:
                            errors.append(err)
                        findings += parse_semgrep(out)
                        ran.append("semgrep")

            root = workspace.root
            triaged = triage(
                findings, root, LessonStore(root).suppressions(), load_fingerprints(root / BASELINE_FILE),
            )
            original_findings = [f.__dict__.copy() for f in findings]
            findings = triaged.findings
            self._remember_scan(root, set(triaged.fingerprints.values()))

            counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in SEVERITY_ORDER}
            summary = ", ".join(f"{n} {sev.lower()}" for sev, n in counts.items() if n) or "none"
            lines = [f"Scanners run: {', '.join(ran) or 'none'}", f"Findings: {len(findings)} ({summary})"]
            if triaged.suppressed:
                lines.append(f"Suppressed by {LESSONS_FILE}: {len(triaged.suppressed)} (known false positives)")
            if triaged.has_baseline:
                lines.append(f"New since the last review: {len(triaged.new)} (marked [new]; the rest were seen before)")
            lines += [
                f.render() + (" [new]" if triaged.has_baseline and triaged.is_new(f) else "")
                for f in findings[:MAX_FINDINGS]
            ]
            if len(findings) > MAX_FINDINGS:
                lines.append(f"...[{len(findings) - MAX_FINDINGS} more findings not shown; narrow paths to see them]")
            lines += [f"Note: {n}" for n in notes]
            lines += [f"Error: {e}" for e in errors]
            return ToolResult(
                success=not errors and bool(ran),
                output="\n".join(lines),
                data={"findings": original_findings, "scanner_outputs": scanner_outputs,
                      "scanner_capture_archives": scanner_capture_archives,
                      "suppressed": [f.__dict__.copy() for f, _ in triaged.suppressed],
                      "coverage": {"reviewed": ran, "skipped": notes, "failed": errors,
                                   "unknown": ["Scanner completion does not establish per-file semantic coverage"],
                                   "status": "partial" if errors or notes else "scanner_completed"}},
                duration_ms=int((time.time() - start_t) * 1000),
            )
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time() - start_t) * 1000))


class RecordFindingTool(Tool):
    """Write a finding to the review's ledger (.local-coder/security/ledger.json).

    The ledger is kept outside the conversation, so it survives context
    compaction and carries over between the batches of a long review. It is
    the only thing this tool writes; project files are never touched.
    """

    name = ToolName.RECORD_FINDING
    description = (
        "Record a security finding in the review ledger as soon as you have evidence for it, "
        "and again to update it (same path and rule/title updates the entry). The ledger survives "
        "context compaction, so anything not recorded here may be forgotten. Mark scanner results you "
        "disproved as false_positive with their rule so future reviews can learn to skip them. "
        "Writes only the agent's own state file, never project code."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short name, e.g. 'SQL injection in get_user'"},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "path": {"type": "string", "description": "File relative to the project root"},
            "line": {"type": "integer", "description": "Line of the vulnerable sink"},
            "rule": {"type": "string", "description": "Scanner rule id if a scanner flagged it, e.g. B602 or secrets/hardcoded-credential"},
            "status": {"type": "string", "enum": list(STATUSES), "description": "suspected until you traced the full path"},
            "confidence": {"type": "integer", "minimum": 1, "maximum": 10, "description": "How sure you are it is exploitable (8+ is reported)"},
            "exploit_path": {"type": "string", "description": "Attacker-controlled input and the path it takes to the sink"},
            "fix": {"type": "string", "description": "Concrete fix"},
            "lesson": {"type": "string", "description": "Optional reusable lesson for future reviews of this project"},
        },
        "required": ["title", "severity", "path"],
    }

    def __init__(self, project_root: str):
        self.project_root = project_root

    async def execute(self, title: str = "", severity: str = "MEDIUM", path: str = "", line: int = 0,
                      rule: str = "", status: str = "", confidence: int | None = None, exploit_path: str = "",
                      fix: str = "", lesson: str = "", **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            if not title.strip() or not path.strip():
                raise ValueError("title and path are required")
            workspace = Workspace(self.project_root)
            rel = workspace.relative_path(path)
            severity = str(severity).upper()
            if severity not in SEVERITIES:
                raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
            if status and status not in STATUSES:
                raise ValueError(f"status must be one of {', '.join(STATUSES)}")
            ledger = FindingsLedger.for_project(workspace.root)
            entry, created = ledger.record(LedgerEntry(
                title=title.strip(), severity=severity, path=rel, line=int(line or 0), rule=rule.strip(),
                # Empty status / confidence 0 mean "unchanged" on an update.
                status=status, confidence=max(1, min(10, int(confidence))) if confidence is not None else 0,
                exploit_path=exploit_path.strip(), fix=fix.strip(), lesson=lesson.strip(),
            ))
            verb = "Recorded" if created else "Updated"
            return ToolResult(
                success=True,
                output=f"{verb} {entry.render()}\nLedger: {len(ledger.entries)} finding(s).",
                duration_ms=int((time.time() - start_t) * 1000),
            )
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time() - start_t) * 1000))

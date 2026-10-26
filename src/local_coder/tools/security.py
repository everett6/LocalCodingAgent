"""Security testing and audit tools for the coding agent."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from local_coder.workspace import Workspace
from local_coder.processes import communicate as _communicate, process_options
from local_coder.tools.scan_scope import scan_files
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult


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

    async def execute(
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
            }

            output_text = self._format_findings_markdown(summary)
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
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, **process_options(),
        )
        stdout, stderr = await _communicate(proc, timeout=60)
        if proc.returncode in (0, 1):  # 1 indicates findings in semgrep
            try:
                data = json.loads(stdout.decode("utf-8"))
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
                    data={**summary, "coverage": coverage},
                    exit_code=proc.returncode,
                )
            except Exception:
                return None
        return None

    async def _run_bandit(self, scan_target: Path, files, coverage) -> ToolResult | None:
        cmd = ["bandit", "-f", "json", "-q", *map(str, files)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, **process_options(),
        )
        stdout, stderr = await _communicate(proc, timeout=60)
        try:
            data = json.loads(stdout.decode("utf-8"))
            if proc.returncode not in (0, 1):
                return None
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
                data={**summary, "coverage": coverage},
                exit_code=proc.returncode,
            )
        except Exception:
            return None

    def _run_builtin_sast(self, scan_target: Path, files, coverage) -> list[dict[str, Any]]:
        """Builtin AST-based and pattern security scanner for Python and JS/TS."""
        findings: list[dict[str, Any]] = []


        for file_path in files:
            if file_path.suffix == ".py":
                findings.extend(self._scan_python_file(file_path, coverage))
            elif file_path.suffix in (".js", ".jsx", ".ts", ".tsx"):
                findings.extend(self._scan_js_file(file_path))

        return findings

    def _is_ignored(self, path: Path) -> bool:
        ignore_parts = {".git", ".venv", "venv", "node_modules", "__pycache__", ".local-coder", "build", "dist"}
        return any(part in ignore_parts for part in path.parts)

    def _scan_python_file(self, file_path: Path, coverage) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        try:
            rel_path = str(file_path.relative_to(self.project_root))
            content = file_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(content, filename=str(file_path))
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            coverage["skipped"].append(f"{file_path.name}: {type(exc).__name__}")
            coverage["status"] = "partial"
            return findings

        from local_coder.tools.detection import python_candidates
        return python_candidates(content, rel_path)

    def _scan_js_file(self, file_path: Path) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        try:
            rel_path = str(file_path.relative_to(self.project_root))
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return findings

        in_block_comment = False
        for i, line in enumerate(content.splitlines(), start=1):
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
            return f"### SAST Scan: Static review\nEngine: `{summary.get('engine')}`\nTarget: `{summary.get('scanned_path')}`\nNo matching static candidates detected; runtime exploitability was not checked."

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
                evidence = {key: f[key] for key in ("source_trace", "sink", "function", "callers", "guards", "decorators", "preconditions", "coverage_limits") if key in f}
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

    def _scan_configs(self, target: Path) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        files, coverage = scan_files(self.project_root, target)

        for file_path in files:
            name = file_path.name.lower()
            rel_path = str(file_path.relative_to(self.project_root))

            # 1. Secret Scanning across text files
            if file_path.suffix in (".py", ".js", ".ts", ".env", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf") or name.startswith(".env"):
                findings.extend(self._scan_for_secrets(file_path, rel_path))

            # 2. Dockerfile security checks
            if "dockerfile" in name:
                findings.extend(self._scan_dockerfile(file_path, rel_path))

            # 3. Environment & config settings
            if name.startswith(".env") or file_path.suffix in (".yaml", ".yml", ".json"):
                findings.extend(self._scan_config_settings(file_path, rel_path))

        return findings, coverage

    def _is_ignored(self, path: Path) -> bool:
        ignore_parts = {".git", ".venv", "venv", "node_modules", "__pycache__", ".local-coder", "build", "dist"}
        return any(part in ignore_parts for part in path.parts)

    def _scan_for_secrets(self, file_path: Path, rel_path: str) -> list[dict[str, Any]]:
        findings = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
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

    def _scan_dockerfile(self, file_path: Path, rel_path: str) -> list[dict[str, Any]]:
        findings = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
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

    def _scan_config_settings(self, file_path: Path, rel_path: str) -> list[dict[str, Any]]:
        findings = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
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

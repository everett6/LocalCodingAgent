"""Conservative AST bug candidates; never executes repository code."""
from __future__ import annotations

import ast
import asyncio

from local_coder.tools.base import Tool
from local_coder.tools.scan_scope import read_scan_source, record_scan_skip, scan_files
from local_coder.security_analysis import AnalysisLimit
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace

MAX_BUG_SOURCE_BYTES = 256000
MAX_BUG_AST_NODES = 40000
MAX_BUG_FINDINGS = 500


class RunBugScanTool(Tool):
    name = ToolName.RUN_BUG_SCAN
    description = "Find Python mutable defaults, swallowed exceptions, return-in-finally and blocking async calls. Candidates require reproduction."
    parameters = {"type": "object", "properties": {"target_path": {"type": "string"}}}

    def __init__(self, project_root):
        self.workspace = Workspace(project_root)

    async def execute(self, target_path=".", **kwargs):
        try:
            target = self.workspace.resolve(target_path)
            if not target.exists():
                raise ValueError("Target does not exist")
            return await asyncio.to_thread(self._scan, target)
        except (ValueError, OSError) as exc:
            return ToolResult(success=False, error=str(exc))

    def _scan(self, target):
        files, coverage = scan_files(self.workspace.root, target)
        findings = []
        coverage["reviewed"] = []
        coverage["analysis_limits"] = {"source_bytes": MAX_BUG_SOURCE_BYTES,
                                       "ast_nodes": MAX_BUG_AST_NODES, "findings": MAX_BUG_FINDINGS}
        seen = set()
        for path in files:
            if path.suffix != ".py":
                record_scan_skip(coverage, path.relative_to(self.workspace.root), "Python bug heuristics do not cover this format")
                continue
            try:
                source = read_scan_source(self.workspace.root, path, coverage=coverage)
                if len(source.encode("utf-8")) > MAX_BUG_SOURCE_BYTES:
                    raise AnalysisLimit("Python bug source exceeds the analysis byte limit")
                tree = ast.parse(source)
                for index, _ in enumerate(ast.walk(tree)):
                    if index >= MAX_BUG_AST_NODES:
                        raise AnalysisLimit("Python bug AST exceeds the node limit")
            except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as exc:
                record_scan_skip(coverage, path.relative_to(self.workspace.root), type(exc).__name__)
                continue
            coverage["reviewed"].append(str(path.relative_to(self.workspace.root)))
            def add(node, rule, message):
                identity = (str(path), node.lineno, rule)
                if identity in seen or len(findings) >= MAX_BUG_FINDINGS:
                    return
                seen.add(identity)
                findings.append({"file": str(path.relative_to(self.workspace.root)), "line": node.lineno,
                                 "severity": "MEDIUM", "rule_id": rule, "message": message,
                                 "confidence": "heuristic", "verification": "unverified"})
            for node in ast.walk(tree):
                if len(findings) >= MAX_BUG_FINDINGS:
                    record_scan_skip(coverage, path.relative_to(self.workspace.root), "Finding budget reached; narrow the target")
                    break
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for default in node.args.defaults + node.args.kw_defaults:
                        if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                            add(default, "BUG-MUTABLE-DEFAULT", "Mutable default is shared between calls; reproduce cross-call state leakage.")
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    add(node, "BUG-BARE-EXCEPT", "Bare except catches cancellation and process-exit exceptions.")
                if isinstance(node, ast.Try):
                    for stmt in node.finalbody:
                        if isinstance(stmt, ast.Return):
                            add(stmt, "BUG-RETURN-FINALLY", "Return in finally suppresses exceptions and earlier return values.")
                if isinstance(node, ast.AsyncFunctionDef):
                    pending = list(reversed(node.body))
                    while pending:
                        call = pending.pop()
                        if isinstance(call, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                            continue  # Defining a nested body does not execute that body here.
                        pending.extend(reversed(list(ast.iter_child_nodes(call))))
                        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                            name = f"{call.func.value.id}.{call.func.attr}" if isinstance(call.func.value, ast.Name) else ""
                            if name in {"time.sleep", "requests.get", "requests.post", "subprocess.run"}:
                                add(call, "BUG-BLOCKING-ASYNC", f"{name} blocks the event loop; reproduce concurrent request starvation.")
            if len(findings) >= MAX_BUG_FINDINGS:
                record_scan_skip(coverage, ".", "Finding budget reached; remaining source requires a narrower scan")
                break
        coverage["status"] = "partial" if coverage["skipped"] else "complete"
        text = "### Bug candidates\nStatic heuristics only; no reproduction tests were executed.\n"
        text += "\n".join(f"- {f['file']}:{f['line']} [{f['rule_id']}] {f['message']}" for f in findings)
        text += f"\nCoverage: {coverage['status']}; {len(findings)} candidates."
        return ToolResult(success=True, output=text, data={"findings": findings, "coverage": coverage})

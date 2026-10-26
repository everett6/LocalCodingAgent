"""Conservative AST bug candidates; never executes repository code."""
from __future__ import annotations

import ast
import asyncio

from local_coder.tools.base import Tool
from local_coder.tools.scan_scope import scan_files
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace


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
        for path in files:
            if path.suffix != ".py":
                continue
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (OSError, UnicodeError, SyntaxError) as exc:
                coverage["skipped"].append(f"{path.name}: {type(exc).__name__}")
                continue
            def add(node, rule, message):
                findings.append({"file": str(path.relative_to(self.workspace.root)), "line": node.lineno,
                                 "severity": "MEDIUM", "rule_id": rule, "message": message,
                                 "confidence": "heuristic", "verification": "unverified"})
            for node in ast.walk(tree):
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
                    for call in ast.walk(node):
                        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                            name = ast.unparse(call.func)
                            if name in {"time.sleep", "requests.get", "requests.post", "subprocess.run"}:
                                add(call, "BUG-BLOCKING-ASYNC", f"{name} blocks the event loop; reproduce concurrent request starvation.")
        coverage["status"] = "partial" if coverage["skipped"] else "complete"
        text = "### Bug candidates\nStatic heuristics only; no reproduction tests were executed.\n"
        text += "\n".join(f"- {f['file']}:{f['line']} [{f['rule_id']}] {f['message']}" for f in findings)
        text += f"\nCoverage: {coverage['status']}; {len(findings)} candidates."
        return ToolResult(success=True, output=text, data={"findings": findings, "coverage": coverage})

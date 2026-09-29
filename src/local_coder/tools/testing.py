"""Testing tools for the coding agent."""
import asyncio
from pathlib import Path
from typing import Any
import time

from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult
from local_coder.verification.test_runner import (
    DEFAULT_TIMEOUT_S, FRAMEWORKS, format_report, run_tests,
)


class RunTestsTool(Tool):
    name = ToolName.RUN_TESTS
    description = (
        "Run the project's tests and get a compact result: counts plus each failure's "
        "test name, file:line and message. The framework is auto-detected "
        "(pytest, unittest, jest, vitest, mocha, go, cargo). Use target/filter to rerun "
        "only what you are fixing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Test file, directory or package (default: whole suite)"},
            "filter": {"type": "string", "description": "Only tests whose name matches (pytest -k, jest -t, go -run...)"},
            "rerun_failed": {"type": "boolean", "description": "Rerun only the tests that failed last time (pytest, jest)"},
            "framework": {"type": "string", "enum": ["auto", *FRAMEWORKS], "description": "Override detection"},
            "timeout": {"type": "integer", "description": f"Seconds (default {DEFAULT_TIMEOUT_S})"},
        },
    }

    def __init__(self, project_root: str, max_failures: int = 10):
        self.project_root = project_root
        self.max_failures = max_failures

    async def execute(
        self,
        target: str | None = None,
        filter: str | None = None,
        rerun_failed: bool = False,
        framework: str = "auto",
        timeout: int = DEFAULT_TIMEOUT_S,
        test_path: str | None = None,  # older name for target
        **kwargs: Any,
    ) -> ToolResult:
        start_t = time.time()
        target = target or test_path
        try:
            report = await run_tests(
                self.project_root, framework=framework or "auto", target=target,
                name_filter=filter, rerun_failed=bool(rerun_failed), timeout=timeout,
            )
        except ValueError as e:
            return ToolResult(success=False, output=f"Error: {e}", duration_ms=int((time.time()-start_t)*1000))
        # An empty run only counts as a failure when the model asked for a
        # specific selection: then a typo in target/filter should not pass.
        success = report.ok or (report.no_tests and not target and not filter and not rerun_failed)
        return ToolResult(
            success=success,
            output=format_report(report, max_failures=self.max_failures),
            exit_code=report.exit_code,
            duration_ms=int((time.time()-start_t)*1000),
        )


class BuildTool(Tool):
    name = ToolName.BUILD
    description = "Run build."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Optional build command override"}
        }
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, command: str | None = None, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            root = Path(self.project_root)
            cmd = []
            
            if command:
                cmd = command.split()
            else:
                if (root / "Makefile").exists():
                    cmd = ["make"]
                elif (root / "Cargo.toml").exists():
                    cmd = ["cargo", "build"]
                elif (root / "package.json").exists():
                    cmd = ["npm", "run", "build"]
                elif (root / "go.mod").exists():
                    cmd = ["go", "build", "./..."]
                else:
                    return ToolResult(success=False, output="Could not auto-detect build command.", duration_ms=int((time.time()-start_t)*1000))
                    
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_root
            )
            stdout, stderr = await proc.communicate()
            success = proc.returncode == 0
            
            output = stdout.decode()
            if stderr:
                output += f"\nStderr:\n{stderr.decode()}"
                
            return ToolResult(success=success, output=output.strip() or "No output.", duration_ms=int((time.time()-start_t)*1000))
        except Exception as e:
            return ToolResult(success=False, output=f"Error: {str(e)}", duration_ms=int((time.time()-start_t)*1000))

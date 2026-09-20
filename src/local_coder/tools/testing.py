"""Testing tools for the coding agent."""
import asyncio
from pathlib import Path
from typing import Any
import time

from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult


class RunTestsTool(Tool):
    name = ToolName.RUN_TESTS
    description = "Run tests."
    parameters = {
        "type": "object",
        "properties": {
            "test_path": {"type": "string", "description": "Optional test path"},
            "framework": {"type": "string", "description": "Framework (auto, pytest, jest, etc)"},
            "verbose": {"type": "boolean", "description": "Verbose output"}
        }
    }
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        
    async def execute(self, test_path: str | None = None, framework: str = "auto", verbose: bool = False, **kwargs: Any) -> ToolResult:
        start_t = time.time()
        try:
            root = Path(self.project_root)
            cmd = []
            
            if framework == "auto":
                if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists():
                    framework = "pytest"
                elif (root / "package.json").exists():
                    framework = "npm test"
                elif (root / "Makefile").exists() and "test:" in (root / "Makefile").read_text():
                    framework = "make test"
                else:
                    framework = "pytest" # Default fallback
                    
            if framework == "pytest":
                cmd = ["pytest"]
                if verbose:
                    cmd.append("-v")
                if test_path:
                    cmd.append(test_path)
            elif framework == "npm test":
                cmd = ["npm", "test"]
                if test_path:
                    cmd.extend(["--", test_path])
            elif framework == "make test":
                cmd = ["make", "test"]
            else:
                cmd = framework.split()
                
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

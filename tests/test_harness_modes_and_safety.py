"""Tests for the improved Aider-style harness, 5 safety tiers, OutputFilter, and 3 modes."""
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from local_coder.agents import create_agent
from local_coder.agents.bug_hunter import BugHunterAgent
from local_coder.agents.security import SecurityAuditorAgent
from local_coder.cli.main import cli
from local_coder.git.checkpoints import CheckpointManager
from local_coder.safety import OutputFilter, SafetyHarness, parse_safety_tier
from local_coder.tools import create_tool_registry
from local_coder.tools.filesystem import WriteFileTool
from local_coder.types import (
    AgentMode,
    AgentRole,
    AgentTask,
    ModelResponse,
    ProjectConfig,
    SafetyTier,
    ToolName,
)


def run_async(coro):
    return asyncio.run(coro)


class TestSafetyTiersAndOutputFilter:
    def test_parse_safety_tiers(self):
        assert parse_safety_tier(1) == SafetyTier.TIER_1_PARANOIA
        assert parse_safety_tier("paranoia") == SafetyTier.TIER_1_PARANOIA
        assert parse_safety_tier(2) == SafetyTier.TIER_2_STRICT
        assert parse_safety_tier("strict") == SafetyTier.TIER_2_STRICT
        assert parse_safety_tier(3) == SafetyTier.TIER_3_BALANCED
        assert parse_safety_tier("balanced") == SafetyTier.TIER_3_BALANCED
        assert parse_safety_tier(4) == SafetyTier.TIER_4_AUTO_VALID
        assert parse_safety_tier("auto_valid") == SafetyTier.TIER_4_AUTO_VALID
        assert parse_safety_tier(5) == SafetyTier.TIER_5_YOLO
        assert parse_safety_tier("yolo") == SafetyTier.TIER_5_YOLO

    def test_output_filter_validates_python_syntax(self):
        valid_py = "def add(x: int, y: int) -> int:\n    return x + y\n"
        is_valid, err = OutputFilter.validate_syntax("test.py", valid_py)
        assert is_valid is True
        assert err is None

        broken_py = "def add(x, y:\n    return x + y\n"
        is_valid, err = OutputFilter.validate_syntax("test.py", broken_py)
        assert is_valid is False
        assert "SyntaxError" in err

    def test_output_filter_validates_js_syntax(self):
        valid_js = "function greet(name) { return `Hello, ${name}`; }\n"
        is_valid, err = OutputFilter.validate_syntax("app.js", valid_js)
        assert is_valid is True

        broken_js = "function broken( { console.log('bad');"
        is_valid, err = OutputFilter.validate_syntax("app.js", broken_js)
        assert is_valid is False
        assert "JavaScript SyntaxError" in err

    def test_output_filter_validates_json_and_yaml(self):
        valid_json = '{"name": "agent", "version": 2}'
        is_valid, _ = OutputFilter.validate_syntax("config.json", valid_json)
        assert is_valid is True

        broken_json = '{"name": "agent", unquoted_key: 2}'
        is_valid, err = OutputFilter.validate_syntax("config.json", broken_json)
        assert is_valid is False
        assert "JSON SyntaxError" in err

        broken_yaml = "foo: [bar\n  baz: 1"
        is_valid, err = OutputFilter.validate_syntax("deploy.yaml", broken_yaml)
        assert is_valid is False
        assert "YAML SyntaxError" in err

    def test_tier_1_paranoia_blocks_file_writes(self, tmp_path: Path):
        harness = SafetyHarness(SafetyTier.TIER_1_PARANOIA)
        tool = WriteFileTool(str(tmp_path), safety_harness=harness)
        result = run_async(tool.execute("foo.py", "x = 1\n"))
        assert result.success is False
        assert "Read-Only" in result.output
        assert not (tmp_path / "foo.py").exists()

    def test_tier_3_balanced_catches_bad_syntax_without_corrupting_file(self, tmp_path: Path):
        harness = SafetyHarness(SafetyTier.TIER_3_BALANCED)
        tool = WriteFileTool(str(tmp_path), safety_harness=harness)

        # 1. Write initial good code
        res1 = run_async(tool.execute("calc.py", "def calc():\n    return 42\n"))
        assert res1.success is True
        assert (tmp_path / "calc.py").read_text() == "def calc():\n    return 42\n"

        # 2. Try writing broken syntax
        res2 = run_async(tool.execute("calc.py", "def broken_calc(:\n    return\n"))
        assert res2.success is False
        assert "Output Filter" in res2.output
        # Verify file on disk was NOT corrupted!
        assert (tmp_path / "calc.py").read_text() == "def calc():\n    return 42\n"

    def test_tier_5_yolo_allows_execution(self, tmp_path: Path):
        harness = SafetyHarness(SafetyTier.TIER_5_YOLO)
        allowed, approval = harness.can_modify_files()
        assert allowed is True
        assert approval is False

        allowed_cmd, approval_cmd = harness.can_execute_command("pip install requests")
        assert allowed_cmd is True
        assert approval_cmd is False


class TestGitArchitectureAndUndo:
    def test_ensure_git_repository_and_undo(self, tmp_path: Path):
        # 1. Ensure git repo initialized
        assert CheckpointManager.ensure_git_repository(str(tmp_path)) is True
        assert (tmp_path / ".git").is_dir()

        mgr = CheckpointManager(str(tmp_path))

        # Initial commit so HEAD exists
        subprocess.run(["git", "config", "user.name", "tester"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(tmp_path), check=True)
        test_file = tmp_path / "main.py"
        test_file.write_text("x = 10\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-m", "initial", "--no-gpg-sign"], cwd=str(tmp_path), check=True)

        # 2. Create checkpoint
        cp = mgr.create()
        assert cp.checkpoint_id.startswith("cp_")

        # 3. Model introduces bad edit / file
        test_file.write_text("x = 9999\n")
        bad_file = tmp_path / "bad.txt"
        bad_file.write_text("hallucination")

        # 4. Instant Aider-style /undo
        mgr.undo()

        # 5. Verify repository was completely restored
        assert test_file.read_text() == "x = 10\n"
        assert not bad_file.exists()


class FakeAgentModel:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.config = SimpleNamespace(temperature=0.0, max_tokens=200)

    async def generate(self, messages, **kwargs):
        return ModelResponse(content=self.response_text, tool_calls=[])


class TestOperatingModes:
    def test_bug_hunter_agent_instantiation(self, tmp_path: Path):
        registry = create_tool_registry(str(tmp_path))
        model = FakeAgentModel("Found an edge case with empty list handling.")
        agent = create_agent(AgentRole.BUG_HUNTER, model, registry)

        assert isinstance(agent, BugHunterAgent)
        assert agent.role == AgentRole.BUG_HUNTER
        assert "Bug Hunter" in agent.system_prompt
        assert registry.has_permission(AgentRole.BUG_HUNTER, ToolName.WRITE_FILE)
        assert registry.has_permission(AgentRole.BUG_HUNTER, ToolName.RUN_TESTS)

    def test_coordinator_mode_routing(self, tmp_path: Path):
        CheckpointManager.ensure_git_repository(str(tmp_path))
        config = ProjectConfig(
            mode=AgentMode.BUG_HUNTING,
            safety_tier=SafetyTier.TIER_3_BALANCED,
        )

        from local_coder.orchestrator.coordinator import Coordinator
        coordinator = Coordinator(config, str(tmp_path))
        assert coordinator.safety_harness.tier == SafetyTier.TIER_3_BALANCED

        # Verify undo method exists and works on coordinator
        cp = coordinator.checkpoints.create()
        assert "Rolled back" in coordinator.undo()


class TestCliHarnessCommands:
    def test_cli_undo_command(self, tmp_path: Path):
        runner = CliRunner()
        with patch("local_coder.cli.main._run_undo") as mock_undo:
            result = runner.invoke(cli, ["undo"])
            assert result.exit_code == 0
            mock_undo.assert_called_once()

    def test_cli_hunt_command(self, tmp_path: Path):
        runner = CliRunner()
        with patch("local_coder.cli.main._run_request") as mock_req:
            result = runner.invoke(cli, ["hunt", "Check boundary conditions in auth"])
            assert result.exit_code == 0
            mock_req.assert_called_once()
            args, kwargs = mock_req.call_args
            assert args[0] == "Check boundary conditions in auth"
            assert args[1]["mode"] == "bug_hunting"

"""Cross-branch regressions for scoped tools and the shared safety harness."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from local_coder.redteam import EngagementScope, EngagementStore
from local_coder.agents import create_agent
from local_coder.agents.investigation import EVIDENCE_STANDARD
from local_coder.safety import SafetyHarness
from local_coder.tools import create_tool_registry
from local_coder.tools.redteam import bind_engagement
from local_coder.types import AgentRole, AgentTask, SafetyTier, ToolName, ToolSettings
from local_coder.verification import test_runner


@pytest.mark.parametrize(
    "tier, replacement, message",
    [
        (SafetyTier.TIER_1_PARANOIA, "value = 2", "Read-Only"),
        (SafetyTier.TIER_3_BALANCED, "value = (", "Syntax validation failed"),
    ],
)
def test_registered_edit_tool_preserves_harness_checks(tmp_path, tier, replacement, message):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    registry = create_tool_registry(str(tmp_path), safety_harness=SafetyHarness(tier))

    result = asyncio.run(registry.execute_tool(AgentRole.CODER, "edit_file", {
        "path": "app.py", "old_string": "value = 1", "new_string": replacement,
    }))

    assert not result.success and message in result.output
    assert source.read_text() == "value = 1\n"


def test_configured_roles_cannot_bypass_engagement_capabilities(tmp_path):
    record = EngagementStore(str(tmp_path)).create(EngagementScope(name="Local", objective="Inspect code"))
    registry = create_tool_registry(str(tmp_path))
    bind_engagement(registry, str(tmp_path), record["id"])
    registry.apply_settings(ToolSettings(roles={"red_team": ["run_command", "read_file", "write_file"]}))

    assert registry.has_permission(AgentRole.RED_TEAM, ToolName.REDTEAM_WORKSPACE)
    assert registry.has_permission(AgentRole.RED_TEAM, ToolName.SECURITY_PLAYBOOK)
    assert registry.has_permission(AgentRole.RED_TEAM, ToolName.RECOVER_CONTEXT)
    assert not registry.has_permission(AgentRole.RED_TEAM, ToolName.RUN_COMMAND)
    assert not registry.has_permission(AgentRole.RED_TEAM, ToolName.READ_FILE)


def test_security_baseline_evidence_is_bounded_and_recoverable(tmp_path):
    registry = create_tool_registry(str(tmp_path))
    model = SimpleNamespace(config=SimpleNamespace(temperature=0.0, max_tokens=100))
    agent = create_agent(AgentRole.SECURITY, model, registry)
    task = AgentTask(role=AgentRole.SECURITY, objective="Review", constraints=[EVIDENCE_STANDARD])
    task.context.previous_findings = [
        "Ledger: app.py:12 source -> sink",
        "SAST candidates\n" + "untrusted scan output " * 2000 + "last-source-marker",
        "Configuration candidates",
        "Dependency coverage unavailable",
    ]

    prompt = agent._format_task(task)

    assert "Ledger: app.py:12 source -> sink" in prompt
    assert len(prompt) < 3000
    assert "last-source-marker" not in prompt
    reference = next(iter(registry.context_archives))
    recovered = asyncio.run(registry.execute_tool(AgentRole.SECURITY, "recover_context", {
        "reference": reference, "query": "last-source-marker",
    }))
    assert recovered.success and "last-source-marker" in recovered.output


def test_cancelled_structured_test_runner_cleans_process_tree(tmp_path, monkeypatch):
    """Cancellation must reap the subprocess even when no timeout was reached."""
    process = AsyncMock()
    process.pid = 1234
    entered = asyncio.Event()
    reaped = asyncio.Event()

    async def blocked_output(size):
        assert size == 8192
        entered.set()
        await reaped.wait()
        return b""

    process.stdin = None
    process.stderr = None
    process.stdout = SimpleNamespace(read=AsyncMock(side_effect=blocked_output))
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock(side_effect=lambda proc: reaped.set())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("local_coder.processes.terminate_tree", cleanup)

    async def scenario():
        task = asyncio.create_task(test_runner.run_tests(tmp_path, framework="pytest"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    cleanup.assert_awaited_once_with(process)
    process.stdout.read.assert_awaited_once_with(8192)
    process.communicate.assert_not_awaited()

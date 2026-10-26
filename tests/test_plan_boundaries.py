import asyncio

import pytest

from tests.test_coordinator import make_coordinator
from local_coder.types import AgentRole, AgentTask, TaskPlan, ToolName


@pytest.mark.parametrize('failure', [None, RuntimeError, asyncio.CancelledError])
def test_planning_masks_mutations_and_restores_permissions(tmp_path, failure):
    async def scenario():
        coordinator = make_coordinator(tmp_path)
        registry = coordinator.tool_registry
        saved = {role: set(names) for role, names in registry._permissions.items()}

        async def explore(request):
            for role in AgentRole:
                for tool in (ToolName.WRITE_FILE, ToolName.RUN_COMMAND, ToolName.RUN_TESTS,
                             ToolName.MCP_CALL, ToolName.DELEGATE_TASK, ToolName.FORMAT_CODE):
                    assert not registry.has_permission(role, tool)
            denied = await registry.execute_tool(AgentRole.ORCHESTRATOR, 'write_file',
                                                 {'path': 'forbidden.txt', 'content': 'changed'})
            assert not denied.success
            assert not (tmp_path / 'forbidden.txt').exists()
            if failure:
                raise failure()
            return 'Inspected files'

        async def plan(request, exploration):
            assert registry.has_permission(AgentRole.PLANNER, ToolName.READ_FILE)
            return TaskPlan(objective='Fix parser', tasks=[AgentTask(role=AgentRole.CODER, objective='Fix parser',
                files=['parser.py'], constraints=['Preserve API'], success_criteria=['Parser regressions pass'])])

        coordinator._explore = explore
        coordinator._plan = plan
        if failure:
            with pytest.raises(failure):
                await coordinator.plan_only('Fix parser')
        else:
            result = await coordinator.plan_only('Fix parser')
            assert 'Fix parser' in result
            assert 'Files: parser.py' in result
            assert 'Constraints: Preserve API' in result
            assert 'Verification: Parser regressions pass' in result
        assert registry._permissions == saved

    asyncio.run(scenario())

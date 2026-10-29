"""Research tools remain read-only and cannot widen engagement permissions."""
import asyncio

from local_coder.tools import create_tool_registry
from local_coder.types import AgentRole, ToolName


def test_research_tools_registered_for_local_review_roles_only(tmp_path):
    registry = create_tool_registry(str(tmp_path))
    names = {ToolName.ADVANCED_AUDIT, ToolName.DEPENDENCY_INTELLIGENCE}
    assert all(registry.get_tool(name) is not None for name in names)
    for role in (AgentRole.SECURITY, AgentRole.REVIEWER, AgentRole.EXPLORER, AgentRole.BUG_HUNTER):
        assert names <= {tool.name for tool in registry.get_tools_for_role(role)}
    assert not names.intersection(tool.name for tool in registry.get_tools_for_role(AgentRole.RED_TEAM))
    assert not names.intersection(tool.name for tool in registry.get_tools_for_role(AgentRole.EXPLOIT_VALIDATOR))
    tool = registry.get_tool(ToolName.DEPENDENCY_INTELLIGENCE)
    assert tool.parameters['properties']['action']['enum'] == ['inventory', 'query', 'audit']
    result = asyncio.run(tool.execute(action='import', payload={}))
    assert not result.success

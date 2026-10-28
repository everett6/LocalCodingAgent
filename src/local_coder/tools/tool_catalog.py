"""Explicit bounded discovery of schemas already permitted to the caller."""
from __future__ import annotations

import json
from typing import Any

from local_coder.context.catalog import select_tool_catalog
from local_coder.tools.base import Tool, ToolRegistry
from local_coder.types import AgentRole, ToolName, ToolResult


class ToolCatalogTool(Tool):
    read_only = True
    mutating = False
    retry_safe = True
    name = ToolName.TOOL_CATALOG
    description = 'Discover full schemas for permitted tool names omitted by the bounded catalog. Discovery never grants execution permissions.'
    parameters = {'type': 'object', 'properties': {
        'names': {'type': 'array', 'maxItems': 16, 'items': {'type': 'string', 'maxLength': 100}},
        'query': {'type': 'string', 'maxLength': 1000},
        'char_budget': {'type': 'integer', 'minimum': 256, 'maximum': 16000},
    }, 'additionalProperties': False}

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    async def execute(self, names: list[str] | None = None, query: str = '', char_budget: int = 6000,
                      _caller_role: AgentRole | None = None, **kwargs: Any) -> ToolResult:
        if _caller_role is None:
            return ToolResult(success=False, output='Tool discovery requires an authenticated caller role')
        if getattr(self.registry, 'delegation_depth', 0):
            return ToolResult(success=False, output='Tool discovery is unavailable to delegated leaf agents')
        schemas = self.registry.get_schemas_for_role(_caller_role)
        result = select_tool_catalog(schemas, role=getattr(_caller_role, 'value', str(_caller_role)),
                                     task=query, char_budget=char_budget, requested_names=names or [])
        # Do not return every registered schema merely because there is capacity.
        if names:
            result['schemas'] = [item for item in result['schemas'] if item['function']['name'] in names]
        return ToolResult(success=True, output=json.dumps(result), data=result)

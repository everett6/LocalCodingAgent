"""Configured search dispatch stays bounded and preserves MCP approval gates."""
import asyncio
import json

import pytest

from local_coder.integrations.mcp import MCPError


class SearchManager:
    def __init__(self, payload=None, servers=None, tools=None, denied=False):
        self.payload = payload if payload is not None else {"content": [{"type": "text", "text": json.dumps({"results": [{"title": "Reference", "url": "https://example.test/docs", "content": "Useful evidence"}]})}]}
        self.servers = servers if servers is not None else {"research": {"enabled": True, "read_only_tools": ["tavily_search"]}}
        self.tools = tools if tools is not None else [{"name": "tavily_search", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}}]
        self.denied = denied
        self.calls = []

    def status(self):
        return {"servers": self.servers}

    async def list_tools(self, server):
        self.calls.append(("list", server))
        return self.tools

    async def call_tool(self, server, tool, arguments):
        self.calls.append(("call", server, tool, arguments))
        if self.denied:
            raise MCPError("Human approval is required for this MCP tool")
        return self.payload


def search(manager, **kwargs):
    from local_coder.tools.web_search import WebSearchTool
    return asyncio.run(WebSearchTool(manager).execute(**kwargs))


def test_web_search_adapts_query_and_returns_source_evidence():
    manager = SearchManager()
    result = search(manager, query="current Python docs", max_results=2)
    assert result.success
    assert manager.calls[-1] == ("call", "research", "tavily_search", {"query": "current Python docs", "max_results": 2})
    data = json.loads(result.output)
    assert data["untrusted"] is True
    assert data["results"] == [{"title": "Reference", "url": "https://example.test/docs", "snippet": "Useful evidence"}]


@pytest.mark.parametrize("query_field,limit_field", [("q", "count"), ("searchQuery", "limit")])
def test_web_search_uses_advertised_argument_names(query_field, limit_field):
    manager = SearchManager(tools=[{"name": "brave_web_search", "inputSchema": {"properties": {query_field: {"type": "string"}, limit_field: {"type": "integer"}}, "required": [query_field]}}])
    result = search(manager, query="reference", max_results=3)
    assert result.success
    assert manager.calls[-1][-1] == {query_field: "reference", limit_field: 3}


@pytest.mark.parametrize("servers", [{}, {"off": {"enabled": False}}, {"one": {"enabled": True}, "two": {"enabled": True}}])
def test_web_search_requires_unambiguous_enabled_configuration(servers):
    manager = SearchManager(servers=servers)
    result = search(manager, query="reference")
    assert not result.success
    assert manager.calls == []


@pytest.mark.parametrize("tool", [
    {"name": "search", "inputSchema": {"properties": {"query": {"type": "string"}}}},
    {"name": "web_search", "inputSchema": {"properties": {"query": {"type": "string"}}, "required": ["query", "project_file"]}},
])
def test_web_search_rejects_generic_or_unsatisfied_remote_contract(tool):
    manager = SearchManager(tools=[tool])
    assert not search(manager, query="reference").success
    assert all(call[0] != "call" for call in manager.calls)


def test_web_search_propagates_approval_denial_as_failure():
    result = search(SearchManager(denied=True), query="reference")
    assert not result.success
    assert "approval" in result.output.lower()


@pytest.mark.parametrize("payload", [{"isError": True, "content": [{"type": "text", "text": "failed"}]}, {"results": "malformed"}])
def test_web_search_remote_failure_is_not_an_empty_search(payload):
    assert not search(SearchManager(payload=payload), query="reference").success


def test_web_search_distinguishes_empty_results():
    result = search(SearchManager(payload={"structuredContent": {"results": []}}), query="reference")
    assert result.success
    assert json.loads(result.output)["status"] == "no_results"


def test_web_search_bounds_results_and_preserves_first_url():
    payload = {"structuredContent": {"results": [{"title": "Title" * 1000, "url": f"https://example.test/{index}", "content": "snippet " * 10000} for index in range(20)]}}
    result = search(SearchManager(payload=payload), query="reference", max_results=2)
    assert result.success
    assert len(result.output) <= 8000
    data = json.loads(result.output)
    assert data["truncated"] is True
    assert len(data["results"]) == 2
    assert data["results"][0]["url"] == "https://example.test/0"


@pytest.mark.parametrize("arguments", [{"query": ""}, {"query": "x" * 1001}, {"query": "reference", "max_results": True}, {"query": "reference", "max_results": 6}, {"query": "sk-" + "a" * 40}])
def test_web_search_rejects_invalid_or_secret_query_without_transport(arguments):
    manager = SearchManager()
    assert not search(manager, **arguments).success
    assert manager.calls == []


def test_web_search_is_available_for_research_but_not_local_poc_or_leaf_roles(tmp_path):
    from local_coder.tools import create_tool_registry
    from local_coder.types import AgentRole, ToolName, ToolSettings

    registry = create_tool_registry(str(tmp_path))
    for role in (AgentRole.EXPLORER, AgentRole.PLANNER, AgentRole.CODER, AgentRole.DEBUGGER,
                 AgentRole.REVIEWER, AgentRole.SECURITY, AgentRole.BUG_HUNTER):
        assert registry.has_permission(role, ToolName.WEB_SEARCH)
    assert not registry.has_permission(AgentRole.EXPLOIT_VALIDATOR, ToolName.WEB_SEARCH)
    token = registry.task_context.set({"depth": 1, "task_id": "leaf", "files": ["."]})
    try:
        result = asyncio.run(registry.execute_tool(AgentRole.EXPLORER, "web_search", {"query": "reference"}))
        assert not result.success
        assert "Leaf tasks" in result.output
    finally:
        registry.task_context.reset(token)
    registry.apply_settings(ToolSettings(disabled=["web_search"]))
    assert registry.get_tool(ToolName.WEB_SEARCH) is None


def test_web_search_role_overrides_still_remove_permission():
    from local_coder.tools.base import ToolRegistry
    from local_coder.types import AgentRole, ToolName, ToolSettings

    registry = ToolRegistry()
    registry.apply_settings(ToolSettings(roles={"explorer": ["read_file"]}))
    assert not registry.has_permission(AgentRole.EXPLORER, ToolName.WEB_SEARCH)


def test_web_search_never_fetches_remote_schema_references(monkeypatch):
    def forbidden_fetch(*args, **kwargs):
        raise AssertionError("Search schema validation attempted a separate network fetch")

    monkeypatch.setattr("urllib.request.urlopen", forbidden_fetch)
    manager = SearchManager(tools=[{"name": "web_search", "inputSchema": {
        "type": "object", "properties": {"query": {"$ref": "https://schema.example.test/query.json"}},
    }}])
    assert not search(manager, query="reference").success
    assert all(call[0] != "call" for call in manager.calls)


def test_web_search_never_transmits_configured_credential_in_query(monkeypatch):
    monkeypatch.setenv('CLIN_SEARCH_FIXTURE_TOKEN', 'private-fixture-value')
    manager = SearchManager(servers={'research': {'enabled': True, 'token_env': 'CLIN_SEARCH_FIXTURE_TOKEN'}})
    result = search(manager, query='Find private-fixture-value in docs')
    assert not result.success
    assert manager.calls == []
    assert 'private-fixture-value' not in result.output


def test_web_search_does_not_evaluate_untrusted_regex_schema(monkeypatch):
    from jsonschema import Draft202012Validator
    def forbidden_pattern(*args, **kwargs):
        raise AssertionError('Untrusted search-schema regex was executed')
    monkeypatch.setitem(Draft202012Validator.VALIDATORS, 'pattern', forbidden_pattern)
    manager = SearchManager(tools=[{'name': 'web_search', 'inputSchema': {
        'properties': {'query': {'type': 'string', 'pattern': '(a+)+$'}},
    }}])
    result = search(manager, query='reference')
    assert not result.success
    assert all(call[0] != 'call' for call in manager.calls)


def test_web_search_rejects_local_schema_reference_expansion():
    manager = SearchManager(tools=[{'name': 'web_search', 'inputSchema': {
        '$defs': {'query': {'type': 'string'}},
        'properties': {'query': {'$ref': '#/$defs/query'}},
    }}])
    assert not search(manager, query='reference').success
    assert all(call[0] != 'call' for call in manager.calls)

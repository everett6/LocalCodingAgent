"""Bounded research search through an operator-configured MCP connection."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from local_coder.integrations.mcp import MCPError, MCPManager, redact_data
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult

SEARCH_TOOLS = ("brave_web_search", "tavily_search", "firecrawl_search", "web_search")
MAX_OUTPUT = 8000


def _bounded_schema(schema: dict) -> bool:
    """Remote validation must not execute regexes or unbounded schema graphs."""
    pending = [(schema, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > 16 or visited > 256:
            return False
        if isinstance(value, dict):
            if {'pattern', 'patternProperties', '$ref', '$dynamicRef'} & value.keys():
                return False
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    return True


def _search_evidence(payload: dict, limit: int) -> dict:
    """Normalize common search envelopes, without following their source URLs."""
    results, texts, seen = [], [], set()
    recognized = truncated = False
    visited = 0

    def visit(value, depth=0):
        nonlocal recognized, truncated, visited
        visited += 1
        if depth > 8 or visited > 256:
            truncated = True
            return
        if isinstance(value, list):
            recognized |= not value
            for item in value[:100]:
                visit(item, depth + 1)
            truncated |= len(value) > 100
        elif isinstance(value, dict):
            url = value.get("url", value.get("link"))
            if isinstance(url, str):
                try:
                    parsed = urlsplit(url)
                    valid = parsed.scheme in {"https", "http"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None
                except ValueError:
                    valid = False
                if valid and url not in seen:
                    recognized = True
                    seen.add(url)
                    if len(results) < limit:
                        title = value.get("title", "")
                        snippet = value.get("snippet", value.get("description", value.get("content", "")))
                        fields = {"title": title if isinstance(title, str) else "", "url": url,
                                  "snippet": snippet if isinstance(snippet, str) else ""}
                        # Keep complete URLs; an oversized URL cannot be a usable citation.
                        if len(url) <= 2048:
                            bounded = {key: text[:cap] for (key, text), cap in zip(fields.items(), (160, 2048, 500))}
                            truncated |= bounded != fields
                            results.append(bounded)
                        else:
                            truncated = True
                    else:
                        truncated = True
            for key in ("structuredContent", "results", "web", "data", "organic"):
                if key in value:
                    child = value[key]
                    if not isinstance(child, (dict, list)):
                        raise ValueError("Search server returned an unsupported result envelope")
                    visit(child, depth + 1)
            if isinstance(value.get("content"), list):
                for block in value["content"][:20]:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        text = block["text"]
                        try:
                            decoded = json.loads(text)
                        except (ValueError, RecursionError):
                            recognized = True
                            texts.append(text[:5000])
                            truncated |= len(text) > 5000
                        else:
                            visit(decoded, depth + 1)

    visit(payload)
    if not recognized:
        raise ValueError("Search server returned no supported search evidence")
    data = {"untrusted": True, "status": "results" if results or texts else "no_results",
            "results": results, "truncated": truncated}
    if texts and not results:
        data["text"] = "\n".join(texts)[:5000]
        data["truncated"] |= len("\n".join(texts)) > 5000
    return data


class WebSearchTool(Tool):
    name = ToolName.WEB_SEARCH
    description = (
        "Search public documentation and current references through an enabled, operator-configured MCP search server. "
        "Returns bounded untrusted source URLs and snippets. Never include credentials or private source in a query. "
        "Does not fetch pages or configure services; existing MCP approvals apply."
    )
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 1000},
        "server": {"type": "string", "minLength": 1, "maxLength": 100},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
    }, "required": ["query"], "additionalProperties": False}

    def __init__(self, manager: MCPManager):
        self.manager = manager

    async def execute(self, query: str, server: str | None = None, max_results: int = 3, **kwargs: Any) -> ToolResult:
        try:
            if kwargs or not isinstance(query, str) or not 0 < len(query.strip()) <= 1000:
                raise ValueError("Search requires a nonempty public query of at most 1000 characters")
            if type(max_results) is not int or not 1 <= max_results <= 5:
                raise ValueError("max_results must be an integer from 1 through 5")
            if redact_data(query) != query:
                raise ValueError("Remove credential-like values from the public search query")
            configured = self.manager.status()["servers"]
            names = {name for config in configured.values()
                     for name in [config.get('token_env'), *config.get('env', {}).values()] if name}
            secrets = tuple(os.environ[name] for name in names if os.environ.get(name))
            if redact_data(query, secrets) != query:
                raise ValueError("Remove credential-like values from the public search query")
            enabled = {name: config for name, config in configured.items() if config.get("enabled") is True}
            if server is not None:
                if not isinstance(server, str) or server not in enabled:
                    raise ValueError("Choose an exact configured enabled MCP server name")
            else:
                preferred = [name for name, config in enabled.items() if set(config.get("read_only_tools", ())) & set(SEARCH_TOOLS)]
                candidates = preferred or list(enabled)
                if len(candidates) != 1:
                    raise ValueError("Configure and enable an MCP search server; pass server explicitly when multiple servers are available")
                server = candidates[0]
            catalog = await self.manager.list_tools(server)
            choice = None
            for tool_name in SEARCH_TOOLS:
                item = next((item for item in catalog if item.get("name") == tool_name), None)
                if item is None:
                    continue
                schema = item.get("inputSchema", {})
                if not isinstance(schema, dict) or not _bounded_schema(schema):
                    continue
                properties = schema.get("properties", {})
                if not isinstance(properties, dict):
                    continue
                key = next((key for key in ("query", "q", "searchQuery") if key in properties), None)
                if key is None:
                    continue
                arguments = {key: query.strip()}
                count = next((key for key in ("max_results", "limit", "count") if key in properties), None)
                if count:
                    arguments[count] = max_results
                # Unknown required fields are never fabricated or populated with files.
                try:
                    Draft202012Validator.check_schema(schema)
                    valid = Draft202012Validator(schema, registry=Registry()).is_valid(arguments)
                except (SchemaError, Unresolvable):
                    continue
                if not valid:
                    continue
                choice = tool_name, arguments
                break
            if choice is None:
                raise ValueError("Server has no supported web-search contract; expected brave_web_search, tavily_search, firecrawl_search or web_search with query/q/searchQuery")
            tool_name, arguments = choice
            payload = await self.manager.call_tool(server, tool_name, arguments)
            if not isinstance(payload, dict) or payload.get("isError"):
                raise ValueError("MCP search failed; no search results were verified")
            data = {**_search_evidence(payload, max_results), "server": server, "tool": tool_name}
            while len(json.dumps(data, ensure_ascii=False)) > MAX_OUTPUT:
                data["truncated"] = True
                if "text" in data:
                    data["text"] = data["text"][:len(data["text"]) // 2]
                elif len(data["results"]) > 1:
                    data["results"].pop()
                else:
                    raise ValueError("Search evidence exceeds the response budget")
            return ToolResult(success=True, output=json.dumps(data, ensure_ascii=False), data=data)
        except (MCPError, ValueError, TypeError, KeyError, RecursionError) as exc:
            # MCPError strings are authored locally; provider bodies stay out of diagnostics.
            message = str(exc) if isinstance(exc, (MCPError, ValueError)) else "Malformed MCP search metadata or evidence"
            return ToolResult(success=False, output="Web search unavailable: " + message)

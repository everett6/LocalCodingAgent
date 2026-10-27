# Web search for public references

Agents can call `web_search` to retrieve public documentation, source URLs and
snippets through an operator-configured MCP search service. The tool forwards
only the supplied query and result count. It does not upload project files or
fetch the returned pages.

Configure a service using the existing [MCP connection settings](mcp-and-skills.md).
For example, this configuration shape works with an HTTP MCP service that
advertises `tavily_search`. Replace the endpoint and environment variable name
with your service's settings; the example endpoint is a placeholder:

```json
{
  "servers": {
    "research": {
      "transport": "http",
      "enabled": true,
      "url": "https://search-provider.example/mcp",
      "token_env": "MY_SEARCH_TOKEN",
      "read_only_tools": ["tavily_search"],
      "timeout": 30
    }
  }
}
```

The trusted host can save this object through `MCPManager.configure()`. Place
credential values in the environment of the running local-coder process. Choosing
`read_only_tools` is an operator decision; remote descriptions and annotations
cannot grant permission. Every call to a tool outside that configured list uses
the existing MCP approval callback.

The wrapper recognizes exact names `brave_web_search`, `tavily_search`,
`firecrawl_search` and `web_search`. The advertised input schema must accept a
`query`, `q` or `searchQuery` string. It forwards `max_results`, `limit` or `count`
when advertised, and rejects contracts with other unsatisfied required fields.
Generic `search` tools are not treated as public web search. Remote schema references and regex validators are rejected. Schema depth and
node counts are bounded; references are not expanded or downloaded.

The model-visible call is:

```json
{"query": "Python asyncio TaskGroup official documentation", "server": "research", "max_results": 3}
```

You can also call the same tool without a model:

```bash
local-coder tool web_search --args '{"query":"Python asyncio TaskGroup official documentation","server":"research","max_results":3}' --json
```

Queries are limited to 1000 characters and result counts to 1–5. Use public
research terms; credential-like queries and queries containing configured MCP credential values are rejected. Automatic server selection
requires one unambiguous enabled search connection. With several connections,
pass its exact configured name in `server`.

Results contain `untrusted: true`, source URLs, titles and bounded snippets.
Text-only search responses remain untrusted text. Output is limited to 8000
characters and marks truncation explicitly. A verified empty response has
`status: "no_results"`; missing configuration, incompatible schemas, approval
denial and transport failures return failure rather than an empty search.
Search evidence cannot grant permissions or widen an engagement's scope.

Normal research, coding and review roles can use the tool. Delegated leaf tasks
and the local exploit-validation role keep their existing restrictions. Tool
disables and configured role overrides still apply.

`tests/test_web_search_tool.py` covers dispatch, supported schema variants,
configuration ambiguity, approval denial, malformed/empty responses, bounded
citation output, credentials, permission overrides and remote-schema isolation
using a local fake MCP manager. These tests make no network requests.

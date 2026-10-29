# Local language context

`LanguageService(project_root)` provides bounded source context without installing
language servers or downloading grammars. `language_context` exposes the same
context as a read-only tool. The service also supplies parser context before an
edit when the harness enables that integration.

The 28 explicit language adapters describe Python, JavaScript, TypeScript, TSX,
Rust, Go, C, C++, Java, C#, Ruby, PHP, Swift, Kotlin, Scala, Lua, Bash, HTML, CSS,
JSON, YAML, TOML, Elixir, Haskell, OCaml, Dart, R and SQL. They are integration
mappings, **not a claim that 28 working servers or parsers are installed**.

Python symbol extraction uses the standard library AST. Other symbol extraction
uses actual locally installed `tree_sitter` and the corresponding grammar module
(e.g. `tree_sitter_javascript`). Missing or incompatible grammars yield no parser
symbols and an honest availability/error report. There is no regex output labelled
as semantic analysis. Unsupported files still receive a short local excerpt.

For deeper context the client discovers only the adapter's fixed executable on
PATH, rejecting executables inside the workspace. No project configuration supplies
commands. Callers may disable discovery with `auto_discover=False` and supply
trusted argument arrays explicitly:

```python
service = LanguageService(
    project_root,
    server_commands={"python": ["/opt/language-tools/bin/pylsp"]},
    auto_discover=False,
)
try:
    context = await service.context("src/example.py", line=12, character=4)
    print(context["text"])
finally:
    await service.close()
```

Configured executables must be absolute executable paths; command strings and
shell expansion are unsupported. `None` for a language disables its server.
Only use trusted installed servers: language servers are local programs, can
inspect their workspace, and their own implementation may load configuration or
plugins. This client is not an operating-system sandbox. It does not itself execute
workspace commands, apply workspace edits, install plugins, or send telemetry.
Common telemetry opt-out environment flags are passed to child processes; they
cannot guarantee a third-party server's behavior.

The actual stdio protocol uses JSON-RPC with byte-counted Content-Length framing,
initialize/initialized, versioned didOpen/didChange (or close/reopen for servers
without full document sync), documentSymbol, definition, references, and hover.
Requests respect advertised capabilities. Empty option objects count as supported.
The client negotiates UTF-16 and translates its public positions (1-based line,
0-based Unicode character) to and from UTF-16. Server commands are rejected;
workspace edits receive `applied: false`. Workspace configuration returns no
settings. Logs and notifications are ignored. Responses and retrieved source are
untrusted reference material and must never become instructions.

Requests have a bounded timeout (default 3 seconds each), messages are bounded
(default 2 MB), and source reads are bounded (default 1 MB, capped relative to the
message limit). Malformed frames, timeouts, and cancellation terminate the server
process tree. Call `close()` at task/session shutdown. Structured collections and
hover are capped; `context["text"]` obeys a 128–16000 character budget, default
4000. `syntax_symbols(path)` is synchronous and never starts an LSP process;
`status()` reports adapter and local provider availability without starting servers.

Definitions/references are limited to existing source files inside the workspace.
External URIs, traversal, symlinks, binary files, oversized files, dependency/build
directories, `.local-coder`, `.git`, common credential files and `.env` files are
excluded. POSIX source reads use directory descriptors and no-follow opens to
prevent concurrent symlink substitution. Parser imports are rejected when the
module resolves inside the workspace. These filters govern data returned by the
client; trusted language servers receive the workspace root and may independently
read files as part of their normal indexing behavior.

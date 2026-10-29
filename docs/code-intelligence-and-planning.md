# Code intelligence and planning

The `language_context` tool returns bounded symbols, definitions, references,
hover text, and diagnostics from trusted, locally configured LSP executables.
Python AST and optional installed Tree-Sitter grammars supply structural fallback
context when a language server is unavailable. Servers are not auto-installed.
Use an LSP-speaking wrapper for TypeScript; raw `tsserver` is not an LSP endpoint.
Pyright can be selected with an explicit absolute `pyright-langserver` command
and `--stdio`; the existing default Python adapter discovers `pylsp`.

## Diagnostics

When a server advertises `diagnosticProvider`, the client requests a full
`textDocument/diagnostic` report for the document it just synchronized. Otherwise,
it can use `publishDiagnostics` notifications encountered during its semantic
requests, but only when their document version matches. Unversioned or stale push
notifications are not presented as current evidence. This is request-time
diagnostics, not a continuous background diagnostic subscription. Push-only
servers may report unavailable until a matching notification has been received.

Each report contains at most 50 diagnostics with bounded messages, source names,
severity, and converted Unicode positions. Related-document links are not
followed. If the file changes on disk during a request, diagnostics are discarded
and marked `stale-source`. The server's configured scope and transport limits
continue to apply; this client rejects server requests to apply edits or execute
commands. Diagnostic text remains untrusted evidence, not an instruction.

Availability distinguishes `pull`, `versioned-push`, `unavailable`, and
`stale-source`. An empty unavailable report does not mean the file passed type
checking. Tool text is also subject to its character budget and may omit entries.

## Plan-only workflow

Choose **Plan** in the existing action selector or use the CLI planning action.
During exploration and planning, every role is limited to project-read tools;
shell commands, tests/builds, file mutations, delegation, and MCP calls are
unavailable. Engine-owned state/checkpoints may still be written, and trusted
language servers may run for read-only analysis. Permissions are restored on
success, failure, or cancellation.

The proposal lists each task's role, objective, files, dependencies, constraints,
and verification criteria. Review it before starting a separate execution job.
This does not yet bind execution to an immutable approved plan or provide a
one-click approval-and-execute transaction. A separate run can generate a new
plan; existing per-tool approvals still apply.

## Remaining integration work

| Request | Current scope and remaining gap |
| --- | --- |
| Incremental vector watchers | The lexical code index refreshes incrementally on access; background embedding watchers remain pending. |
| Containers for all commands | Generated helpers have an isolated runner; ordinary shell/test/package tools are not all routed through containers. |
| Rollback after each step | Checkpoints and guarded pre-edit snapshots exist; universal tool rollback and explicit worktree workflows remain separate. |
| Process supervision | Process-group cleanup exists; a unified user-facing server start/log/stop tool remains pending. |
| Test feedback and patches | Test runners, failure feedback, and unified patch tools exist. |
| Automatic lint after edits | Syntax verification exists; configurable debounced ESLint/Ruff/Clippy checks remain pending. |
| Headless GitHub automation | CLI and CI tests exist; automated issue triage and PR review need scoped GitHub integration and publishing controls. |
| MCP and specialist roles | Existing MCP client and planner/coder/reviewer roles are available. |
| Rich TUI and replay | Terminal output, events, checkpoints and optional context snapshots exist; full deterministic replay and a new TUI remain pending. Logs should redact credentials rather than persist every raw payload. |

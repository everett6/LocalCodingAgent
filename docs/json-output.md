# JSON output (`--json`)

Every `local-coder` command except `serve` and interactive mode accepts
`--json`, either before the subcommand (`local-coder --json status`) or after
it (`local-coder status --json`). With it:

- stdout carries exactly one JSON document, the envelope below.
- Everything meant for humans (panels, agent progress events, approval
  prompts) goes to stderr, so `local-coder ... --json | jq` always parses.
- The exit code is `0` when `ok` is true and `1` when it is false.

## Envelope

```json
{
  "schema_version": 1,
  "command": "local-server status",
  "ok": true,
  "data": { },
  "error": null
}
```

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer. Bumped only for breaking changes (a field removed, renamed or retyped). New fields can appear without a bump, so ignore keys you don't know. |
| `command` | The subcommand path, e.g. `"status"`, `"tool"`, `"local-server start"`. Empty if the command itself could not be resolved. |
| `ok` | Whether the command achieved what was asked. See each command for what counts. |
| `data` | Command-specific result (below). `null` when `error` is set. |
| `error` | `null`, or `{"type": "...", "message": "..."}` when the command itself failed (bad arguments, config that won't load, a checkpoint that doesn't exist). `type` is the exception class name, e.g. `ClickException`, `BadParameter`, `UsageError`. |

`ok: false` with `error: null` means the command ran and is reporting a
negative result in `data`: a tool that failed, tests that failed, an agent
run that did not finish.

## Agent runs: `run`, `plan`, `review`, `test`, `resume`

```json
{
  "request": "Fix the failing tests",
  "phase": "run",
  "status": "completed",
  "session_id": "local-1a2b3c4d",
  "result": "Final Report\n...",
  "errors": [ ],
  "events": [
    {"timestamp": "2026-09-29T02:22:22.256453", "source": "ORCHESTRATOR",
     "event_type": "task_started", "message": "Processing: ...", "data": {}, "task_id": null}
  ]
}
```

- `phase` is `run`, `plan`, `review` or `test` (`resume` re-runs as `run`).
- `status` is `completed` or `failed`, from the coordinator's final event.
- `session_id` is the journal entry (see `sessions`); `null` for `test`,
  which does not save one.
- `result` is the same Markdown report the terminal shows.
- `errors` is every event whose `event_type` ends in `error`
  (`model_error`, `plan_error`, `fix_loop_error`, ...).
- `events` is the full progress log in order.
- `ok` is true only when `status` is `completed` and no `model_error`
  occurred (a model call that failed after all retries).

Approval prompts for risky actions are written to stderr. If stdin is not a
terminal, a risky action is denied (and the denial shows up in the events)
rather than hanging; pass `--yolo` to auto-approve in scripts.

## Running a single tool: `tool NAME`

Runs one of the agent's tools directly, with no model involved:

```bash
local-coder tool run_tests --json
local-coder tool grep -a pattern=TODO -a path=src --json
local-coder tool read_file --args '{"path": "README.md", "start_line": 1}' --json
```

`-a KEY=VALUE` is repeatable and its value is parsed as JSON when possible
(`-a start_line=1` is the number 1), else kept as a string. `--args` takes
the whole argument object; `-a` values are applied on top of it.

```json
{
  "tool": "run_tests",
  "arguments": {},
  "success": false,
  "output": "...",
  "error": null,
  "exit_code": null,
  "duration_ms": 812.0,
  "files_changed": [],
  "details": {"failures": [{"test": "tests/test_a.py::test_x", "message": "assert 1 == 2"}]}
}
```

`ok` equals `success`. `details` is extra structure parsed from the output
for tools that have a parser (`run_tests` today, giving each failing test
and its message) and `null` otherwise. An unknown tool name or bad
`--args` is an `error` (`BadParameter`). Any tool registered in
`local_coder.tools.create_tool_registry` is reachable this way, so new tools
get JSON output without extra CLI code.

## `tools`

```json
{"tools": [{"name": "grep", "description": "...", "parameters": {"type": "object", "properties": {}},
            "roles": ["explorer", "planner", "coder", "..."]}]}
```

`parameters` is the JSON Schema the model sees; `roles` lists the agent
roles allowed to call the tool.

## Other commands

| Command | `data` |
| --- | --- |
| `status` | `{"project_root", "models_configured": int or null, "config_error": str or null, "git_branch": str or null, "ollama": {"reachable": bool, "status_code": int or null}}` |
| `agents` | `{"agents": [{"role", "model": str or null, "system_prompt"}]}`; `system_prompt` is the prompt's first line |
| `models` | `{"models": [{"name", "backend", "model_id", "base_url", "context_length", "temperature", "max_tokens"}]}` |
| `sessions` | `{"sessions": [ {"session_id", "request", "phase", "result", "updated_at", ...} ]}`, newest first |
| `init` | `{"config_path"}` |
| `checkpoint`, `rollback` | `{"checkpoint": {"checkpoint_id", "created_at", "head", "patch_file", "untracked_dir"}}` |
| `checkpoints` | `{"checkpoints": [ ...same shape... ]}`, newest first |
| `local-server models` | `{"models": [{"name", "path", "size_gb", "note"}]}` |
| `local-server status` | `{"servers": {"big": {"pid", "process_running", "port", "healthy"}, "draft": {...}}}` |
| `local-server start` | `{"big": {...}, "draft": {...} or null, "draft_error": str or null, "healthy": bool or null}`; `ok` is false if `--wait` found the big model unhealthy |
| `local-server switch` | `{"big": {...}, "healthy": bool or null}`; same `ok` rule |
| `local-server stop` | `{"big": {...} or null, "draft": {...} or null}` |

## Adding `--json` to a new command

Put `@json_option` (from `local_coder.cli.output`) under the command
decorator, then at the end of the command:

```python
if json_mode(ctx.obj):
    emit(ctx.obj, {"...": ...}, ok=...)
    return
```

Raise `click.ClickException` for failures; the root group turns it into an
error envelope in JSON mode. Add the command's `data` shape to this file.

# Local Coding Agent

Local Coding Agent is a Python coding agent for local model backends. It combines repository context, role-specific tools, and a bounded model/tool loop that can inspect files, apply edits, run tests, and feed results back into the model.

## Install

Install the command into your Python environment:

```bash
pip install -e "."
```

This provides both `local-coder` and the shorter `lc` command. It can also be
run without an installed script with `python -m local_coder`.

## Requirements

- Python 3.12+
- A local OpenAI-compatible, Ollama, or llama.cpp model server
- `pip install -e ".[dev]"` for development dependencies

## Run

From the project you want the agent to work on, create a local configuration:

```bash
cd your-project
local-coder init
```

Edit `.local-coder/config.yaml` with the URL and model ID for your local model
server, then run:

```bash
local-coder "Fix the failing tests"
local-coder plan "Add authentication"
local-coder test
local-coder review
local-coder checkpoint
local-coder checkpoints
local-coder rollback <checkpoint-id>
local-coder --version
```

Running `local-coder` with no arguments opens the interactive terminal mode.
The root command also accepts a direct request, so `lc "Fix the failing tests"`
is equivalent to `local-coder run "Fix the failing tests"`.

## Agentic workflow

The framework follows a plan, execute, review, verify loop. A planner can
assign smaller tasks to coder, tester, debugger, or reviewer agents through the
task DAG, and each task may select a different configured model with
`model_name`. Role defaults can be configured in `.local-coder/config.yaml`:

```yaml
agentic:
	role_models:
		planner: reasoning-model
		coder: coding-model
		reviewer: review-model
	context_window_chars: 24000
	compact_context_chars: 12000
```

Long tool histories are compacted automatically while the system and task
context remain available. Use `local-coder --model coding-model "..."` to route
one run to a selected model.

Independent tasks in a plan (no `depends_on` between them) run concurrently,
bounded by `agentic.max_parallel_agents` in the config (default `1`, i.e.
sequential). Raise it to have several coder/tester/reviewer agents working
different parts of a plan at the same time.

For remote control from another terminal or machine on a trusted network,
start the explicitly opt-in loopback server:

```bash
local-coder serve --host 127.0.0.1 --port 8787
curl http://127.0.0.1:8787/status
curl -X POST http://127.0.0.1:8787/plan \
	-H 'Content-Type: application/json' \
	-d '{"request":"Add authentication","session_id":"auth-plan"}'
curl -X POST http://127.0.0.1:8787/run \
	-H 'Content-Type: application/json' \
	-d '{"request":"Implement the plan","session_id":"auth-run"}'
curl http://127.0.0.1:8787/events
local-coder sessions
local-coder resume auth-run
```

The remote server binds to `127.0.0.1` by default. A non-loopback bind requires
a token, supplied directly or through `LOCAL_CODER_REMOTE_TOKEN`:

```bash
local-coder serve --host 0.0.0.0 --port 8787 --token "$LOCAL_CODER_REMOTE_TOKEN"
curl -H "Authorization: Bearer $LOCAL_CODER_REMOTE_TOKEN" \
	http://127.0.0.1:8787/status
```

Use an authenticated tunnel or reverse proxy as an additional boundary before
exposing the service outside a trusted network.

The default configuration expects an OpenAI-compatible server at `http://localhost:8090/v1`. Change the endpoint and model ID to match your local server.

Checkpoints save the current Git working tree locally under `.local-coder/checkpoints/`. They do not create commits or push anything. Use them before autonomous edits, then restore one with `local-coder rollback <checkpoint-id>` if needed.

## Safety

All filesystem paths are resolved relative to the selected project workspace. Parent traversal and symlink escapes are rejected. Shell commands are classified as safe, approval-required, or blocked; tests and read-only inspection commands are safe by default, while package installation, Git mutation, privilege escalation, and destructive commands are restricted.

Approval-required actions (an ASK-risk shell command, a git commit, or a git
checkout) pause for a real decision instead of just failing:

- Interactively (`local-coder ...`), you get a y/n prompt in the terminal
  showing exactly what the agent wants to run.
- Pass `--yolo` to auto-approve every risky action for that invocation
  without prompting (equivalent to Claude Code's
  `--dangerously-skip-permissions` or Cursor's auto-run) -- only use it in a
  sandbox or on a branch you don't mind the agent breaking.
- Headless/remote runs (`local-coder serve`, or any use of `Coordinator`
  without wiring an approval callback) deny approval-required actions by
  default; pass `--yolo` when starting `serve` to opt that server into
  auto-approval explicitly.

This is controlled by `approval.require_approval_for_commands` and
`approval.require_approval_for_commits` in the config, both `true` by
default.

Agent runs are bounded by iteration, tool-call, and test-run limits. Verification failures are summarized before being passed to the debugger, and the final report records whether verification passed or exhausted its retry budget.

## Predictive drafting

The speculative drafter is an application-level latency optimization. `DeltaAttention` prioritizes changed and task-relevant lines in the predictor prompt, while `PredictionPolicy` learns online from accepted and rejected drafts and disables prediction types that stop paying for their own latency. This is intentionally distinct from native transformer DeltaNet/Delta-attention kernels or inference-engine speculative decoding; those require model and backend support and are not emulated by the agent runtime.

Repeated speculative prompts use a bounded model-aware TTL/LRU cache. Mutable coding-agent tool responses are never cached, so edits and test results cannot become stale. Backend-native KV or prefix caching can be added later behind the provider interface when a local inference server exposes a stable API for it.

## Architecture

- `src/local_coder/agents/`: role-specific agents and the bounded tool loop
- `src/local_coder/tools/`: filesystem, search, shell, Git, and test tools
- `src/local_coder/context/`: repository discovery and task context
- `src/local_coder/models/`: local model backend adapters
- `src/local_coder/orchestrator/`: planning, execution, review, and test/fix coordination

Run the tests with:

```bash
PYTHONPATH=src pytest -q
```
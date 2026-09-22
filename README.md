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

## Local model server

This repo's own `config/config.yaml` is set up for the local llama.cpp stack
in `~/AI2` (Qwen3-30B-A3B-Instruct-2507, OpenAI-compatible API on
`http://localhost:8090/v1`) already on this machine. `local-coder` manages
that server for you -- no standalone launcher script needed -- via the
`local-server` command group (`src/local_coder/local_server.py`), which
launches `llama-server` as a detached background process and tracks it by
PID under `~/.local-coder/local_server/`:

```bash
local-coder local-server models              # quants present on disk, with notes
local-coder local-server start                # big model, default quant (q2_k, fastest)
local-coder local-server start --quant ud-q3_k_xl --n-ctx 65536
local-coder local-server status               # pid/port/health for big + draft
local-coder local-server switch --quant iq3_xxs   # stop + restart with a different quant
local-coder local-server stop                  # stop both big and draft servers
```

By default `start` also launches a second, genuinely separate small model
(Qwen2.5-Coder-0.5B, CPU-only, port 8091) that `config/config.yaml`'s
`drafter` entry points at -- see "Predictive drafting" below for why that
has to be its own process rather than another config entry pointing at the
same 30B server. Pass `--no-draft` to skip it.

```bash
curl http://localhost:8090/health
local-coder "Add a docstring to a math helper"
```

**About the 65536-token context (not 500000):** `models.*.context_length` and
`agentic.context_window_chars`/`compact_context_chars` in `config/config.yaml`
are sized to the server's real `-c 65536` launch flag, not the 500k that was
asked for once. That's a hardware ceiling, not a software one: AI2's own
measured cost curve (`AI2/config.py`, `Runtime.n_ctx`) shows the KV cache
cost accelerating with context length -- going from 8192 to 32768 already
pushed 13 of 48 expert layers off the 12GB card. Extrapolating that curve,
500,000 tokens would need on the order of 48GB of KV cache alone, which
doesn't fit in this card's VRAM or even this machine's 32GB of RAM combined.
65536 is a reasonable, tested ceiling for this model on this GPU; going
further (96k-128k) is plausible but untested -- `AI2_N_CTX=<value>` in the
launcher above, then watch `server.log` for a `load_failed` exit if it
doesn't fit.

If you don't have this local stack, point `config/config.yaml` at any other
OpenAI-compatible, Ollama, or llama.cpp server instead.

### Session cache (disk-backed KV cache)

llama-server already reuses a slot's KV cache automatically, in memory,
across requests within the same process (`--cache-prompt`, on by default) --
that part needs no code and no config. What it *doesn't* survive is a
server restart: a crash, an intentional restart to change `n_ctx` or the
quant, or just a reboot throws away everything that was prefilled,
including this project's own exploration step, whose prefix (system prompt
+ repository structure) is usually large and near-identical across runs.

`agentic.session_cache: true` (set in this repo's `config/config.yaml`)
closes that gap: `Coordinator._explore()` calls llama-server's `/slots`
API to restore a saved KV state from disk (under `AI2/state/slot_cache/`,
launcher above) before exploring, and save it back after. A later run,
even against a freshly restarted server, skips re-computing whatever
prefix it already saved -- verified end-to-end: a real run against this
project wrote a 96MB slot file, and restoring it into a completely fresh
`llama-server` process (killed and relaunched) succeeded.

This only works against a llama-server started with `--slot-save-path`
(see the launcher above); it's a no-op on any other backend (Ollama, or a
llama-server without that flag) -- `OpenAICompatibleBackend.save_slot`/
`restore_slot` fail closed (return `False`, never raise) so its absence
never breaks a run. The cache key is a hash of the project root, so
multiple projects sharing one server don't collide.

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

**Stagnation guard:** a small local model is far likelier than a frontier one
to retry an identical failing tool call instead of changing approach. The
tool-calling loop (`agents/base.py`) tracks each call's (name, arguments)
signature: after 3 identical failures in a row it injects a corrective
system message telling the model to stop repeating the call and try
something else; after 5 it gives up on the task rather than silently
burning the rest of the iteration budget on a call that has never once
succeeded.

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

The speculative drafter is an application-level latency optimization, wired
into real Coder/Debugger task execution (`Coordinator._get_drafter()`, used
by `BaseAgent.execute()`) -- not just a benchmark-only code path. Before a
Coder or Debugger agent starts its tool loop on a task that names a file, a
small, separate drafter model (see `local-server` above; `config.yaml`'s
`drafter` entry, default port 8091) is asked for a quick guess at the edit,
which is injected as an unverified starting point rather than applied
directly; whether the agent's actual final edit matched feeds back into
`PredictionPolicy` (see below) as an accept/reject signal. Deliberately a
*different*, smaller model/process from the main one: pointing `drafter` at
the same 30B server would make the draft call compete for the one GPU slot
the main agent call also needs, instead of running cheaply alongside it.

`DeltaAttention` builds the predictor's own prompt by scoring every line of
the target file (diff markers, definition lines, and whole-word matches
against the task objective) and keeping the top-scoring lines *plus a small
window of surrounding context* around each one, so the drafter sees coherent
snippets instead of isolated, disconnected lines. `PredictionPolicy` learns
online from accepted and rejected drafts and disables prediction types that
stop paying for their own latency. This is intentionally distinct from
native transformer DeltaNet/Delta-attention kernels or inference-engine
speculative decoding; those require model and backend support and are not
emulated by the agent runtime.

Repeated speculative prompts use a bounded model-aware TTL/LRU cache (`models/cache.py`). Mutable coding-agent tool responses are never cached, so edits and test results cannot become stale. Backend-native KV or prefix caching can be added later behind the provider interface when a local inference server exposes a stable API for it.

The cache is bounded on two axes: entry count and total tracked size
(`max_total_chars`), since one oversized cached value (a role configured
with a large `max_tokens`) could otherwise make the cache much heavier than
its entry count suggests. Cache keys are hashed incrementally, message by
message, rather than by first building one large JSON string for the whole
prompt -- with the large context window above, a single prompt can be
hundreds of thousands of characters, and materializing that as one string
just to hash it and throw it away was wasted allocation on every drafter
call.

## Browser UI

`local-coder serve` starts the same HTTP control plane used for remote
terminal sessions, and now also serves a single-page browser UI
(`src/local_coder/webui/index.html`) at `/`. It's a Claude-Code-style chat
view -- session list, a live color-coded event stream per agent role, and
Run/Plan-only/Review-changes actions -- talking to the existing JSON API
(`/status`, `/events`, `/plan`, `/run`, `/review`) via `fetch()`. There's no
native Linux/Windows/macOS app or per-OS build: any modern browser on any of
the three renders the same page identically, so this is the "app" for all
of them.

```bash
local-coder serve --project . --port 8787   # add --yolo to auto-approve risky actions
```

Then open `http://localhost:8787` (or `http://<host>:8787` for a
non-loopback bind, which requires `--token` -- paste the same token into the
page's token field; it's sent as an `Authorization: Bearer` header on every
API call and kept in `localStorage`).

## Generation retries

A quantized local model occasionally produces a tool call whose arguments
aren't valid JSON; llama-server's own parser rejects that with an HTTP 500
(`"Failed to parse tool call arguments as JSON"`) rather than repairing or
truncating it. `BaseAgent._execute_loop()` treats a `model.generate()`
failure as retryable up to `generation_retry_limit` extra attempts (default
2, short backoff between them via `generation_retry_backoff_seconds`)
instead of failing the whole task over one bad sample, raising the sampling
temperature a little on each retry (`+0.15` per attempt, capped at `1.0`) so
a retry actually resamples instead of very likely reproducing the same
output. Only the whole-task failure path changes here -- the task is marked
`FAILED` only once every attempt has been exhausted, and the recorded error
names the attempt count.

This does not cure every case: reproducing this against the real server
here, the same request failed identically across all attempts, temperature
increases included -- the log showed the exact same parse error (`"last
read: '{'"`, i.e. a doubled leading brace) every time, which points at a
deterministic bug in llama-server's tool-call grammar/chat-template for that
specific tool schema rather than sampling noise. That's a bug in
llama-server itself (or the GGUF's chat template), out of reach from this
repo's Python code; the retry still helps for genuinely transient failures
(a real sampling glitch, a momentary server hiccup), just not this specific
deterministic one.

## Architecture

- `src/local_coder/agents/`: role-specific agents and the bounded tool loop
- `src/local_coder/tools/`: filesystem, search, shell, Git, and test tools
- `src/local_coder/context/`: repository discovery and task context
- `src/local_coder/models/`: local model backend adapters
- `src/local_coder/orchestrator/`: planning, execution, review, and test/fix coordination
- `src/local_coder/local_server.py`: start/stop/status/switch for this machine's `llama-server` process(es) and model choice (`local-server` CLI group)
- `src/local_coder/webui/`: the browser UI served by `local-coder serve`

Run the tests with:

```bash
PYTHONPATH=src pytest -q
```
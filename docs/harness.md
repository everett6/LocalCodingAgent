# Investigation harness

Use the manager's mode menu, or `/hunt`, `/security`, `/audit`, `/plan`, `/review`
and `/build`. Explicit `@relative/path.py` mentions attach bounded source with
line numbers. Workspace escapes, credential/state attachments and oversized
files are rejected. A follow-up can include the previous task's report as
untrusted context.

Security starts with local static/config/dependency coverage; bug hunting starts
with AST boundary/error/async candidates. These are hypotheses. The specialist
must inspect callers and guards, attempt to disprove candidates, distinguish
unverified/reproduced/fixed-and-verified, and give file/line evidence, triggering
conditions, a regression test and remediation. Default investigations cannot
write files, run arbitrary commands or execute project tests. **Apply fixes & run
tests** enables those tools under the normal approval policy.

Source scans prune generated/vendor directories, skip symlinks and large files,
and cap file count/total bytes. Invalid Python and skipped files make coverage
partial. Missing advisory tools or lockfiles are reported as not checked;
unpinned version ranges do not produce invented CVEs. Third-party scanners are
bounded by a timeout and their process trees are terminated on cancellation.

## GLM 5.3 profile

The `glm53` OpenAI-compatible profile uses enabled thinking, preserved thinking
(`clear_thinking: false`) and maximum reasoning effort. Assistant protocol state
is returned unchanged on tool follow-ups. The default UI preset uses the Z.ai
Coding Plan endpoint; change it to the endpoint your account supports.
See [GLM 5.3](https://docs.z.ai/guides/llm/glm-5.3) and
[thinking mode](https://docs.z.ai/guides/capabilities/thinking-mode).

## Qwen 3.8 profile

The `qwen38` profile enables thinking and requests streaming. The backend assembles
indexed name/argument fragments and usage into a complete response before any
tool executes. Malformed or non-object JSON arguments fail instead of becoming
empty executable arguments. Endpoint/region and model ID remain editable.
See [Model Studio function calling](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling).

Both profiles allow 40 investigation rounds/160 tool calls, bounded by context,
time and test budgets. Compaction retains complete tool exchanges and unmodified
protocol reasoning. If the latest required exchange still exceeds the context
budget, the task stops with an explicit error. This is transport/harness support,
not a claim of measured model accuracy or benchmark improvement.

## Lifecycle and verification

Agent shell commands can optionally run in a disposable Docker or Podman
container. Enable with `tools.command_sandbox: true`; choose
`tools.sandbox_runtime: auto|docker|podman` and a locally available
`tools.sandbox_image`. The default image is `python:3.12-slim`; images are not
pulled automatically. The container has no network, a read-only root
filesystem, dropped capabilities, no-new-privileges, bounded memory/CPU/PIDs,
and a temporary `/tmp`. Only the project workspace is mounted, read-write,
because coding commands need to edit it. A timeout or cancellation requests
container removal. Missing runtimes and cleanup failures are surfaced.

This setting currently applies only to the arbitrary shell-command tool. It
does not sandbox the dedicated test, build, formatter, scanner, or model-server
processes, nor does container isolation guarantee protection from container
runtime vulnerabilities or a misconfigured daemon. Leave it disabled unless
Docker or Podman is installed and the project has a suitable local image.

Each served workspace has one OS-backed engine lease and allows one running job. A second independent engine is rejected; the desktop manager and IDE share an explicit authenticated connection. Activity and reports are retained
in the local session journal; job failure state survives the live event ring.
Closing an owning host first requests authenticated engine shutdown and drains active jobs. Stop cancels model work and reaps agent-owned command/test/build process trees
before releasing the workspace. Review the diff for partial edits after stopping.

Full-round checkpoints save task routing, messages, tool/test limits and outcomes.
Resume continues the saved agent step; it does not reconstruct an enclosing task DAG. Reconnect the same provider after an engine restart before resuming. Resume uses the latest safe boundary in the selected workspace's state directory.
An incomplete model response or interrupted tool batch is never replayed as a
completed exchange. A checkpoint cannot undo an external side effect; always
inspect partial workspace changes before resuming.

Complete agent-round checkpoints also fingerprint readable files recorded in
the agent's context. Resume refuses when one of those files has changed or
disappeared, because the saved conversation would otherwise contain stale
source. Start a new task to rebuild context against the updated workspace.
Older checkpoints without these fingerprints remain resumable under their
existing validation rules.

Failed coding tasks can opt into workspace rollback with
`verification.rollback_on_failure: true`. It is off by default because it can
discard useful partial edits; cancelled tasks are not automatically rolled
back. Before restoring, the engine requires the task's pre-run checkpoint to
remain newest and Git `HEAD` to be unchanged. This does not undo external
command side effects or prevent concurrent manual edits, so use the option only
when the workspace is not being edited elsewhere during a run.

## Local code context and edits

The local retrieval index chunks Python with the AST and other supported source
files at definition boundaries, then ranks chunks with dependency-free BM25.
The RepoMap adds symbol-reference/PageRank structure. Repeated interactive
searches refresh the index at most once per second; explicit `CodeIndex.refresh()`
remains immediate. The remote engine also owns a native `watchfiles`/Rust
filesystem watcher for the workspace lifetime. Batched create, update and
delete events re-index only affected files and update the code-derived session
cache identity; `.git`, generated/dependency directories, state files, binary
formats, oversized files and paths outside the workspace stay excluded.
Watcher startup failures are explicit, and the engine stops/joins the watcher
when its workspace lease is released. CLI-only runs retain on-demand refresh.
This does not promise a sub-200 ms latency guarantee. The current pipeline does
not use embeddings or a learned reranker.

Unified-diff edits validate all target paths and dry-run the complete patch
before applying it. If the real patch application fails partway through, the
tool restores affected files from its pre-apply snapshots. This protects the
patch operation itself; it does not roll back commands or other external side
effects. Task checkpoints remain the workspace recovery mechanism.

Adapter, process lifecycle and end-to-end tests use synthetic projects and fake
model responses. No paid Anthropic/GLM/Qwen account request is needed to run them.

## Red team engagements

Choose **Red Team** in the manager or **Local Coder: Red Team Engagement** in the
IDE. Create a scope with local paths and exclusions, or reuse a saved engagement.
Source inventory, reads and static scans use that saved scope; `@file` mentions
do not attach source outside it. Red team tools have their own capability set,
independent of safety-tier command approvals. Fixes and test execution remain
separate coding/security remediation tasks.

Optional HTTP observations require exact localhost/loopback origins and an
authorization/RoE reference. Remote hosts and public IPs are rejected; enter a
local lab reference, never a credential. GET, HEAD and OPTIONS are supported
without query payloads, cookies or credentials. Origins are pinned when the
engagement is created; any non-loopback DNS answer or later address expansion
is rejected. Each request connects to an allowed pinned IP while preserving TLS verification/SNI and Host.
There are no redirects or environment proxies. The agent cannot generate or run
exploits, fuzzing campaigns, shell commands, persistence, credential tests,
lateral movement, destructive checks, or remote probes. Limits: 50 requests maximum per
engagement, a 5-second DNS check plus a 10-second HTTP exchange, and 64 KiB of response bytes hashed. Raw
response bodies and Set-Cookie headers are not saved. A failed/interrupted attempt
still consumes budget. Safety Tier 1 blocks engagements with active HTTP enabled.

The agent maintains an evidence-led assessment and persistent checklist.
Findings link to evidence IDs in the same
engagement and retain attack-path hypotheses, detection expectations and
remediation. HTTP observations and scanner candidates stay **unverified**;
detection outcomes stay **not_tested** because this mode does not ingest detector
telemetry. It does not execute exploits, credential attacks, persistence or
lateral-movement procedures. Missing security headers alone are not confirmed
vulnerabilities.

Bug-hunting investigations also map workflow states, actors, transition
operations and source-backed guards for API, account, payment and authorization
paths. Reports should distinguish invalid transition sequences from valid
control sequences and propose bounded local regression tests. This is
state-machine analysis, not a live-service probe or an exploit-generation loop.

Import existing SARIF 2.1.0 or Nuclei JSONL artifacts with the engagement tools.
Every imported location must be in scope. Imports are bounded to 1 MB and 100
locations; relative SARIF source locations are supported, remote/base-ID locations
are rejected. Nuclei `matched-at` locations must be exact allowed HTTP origins
without query payloads. No scanner templates are executed. Imported findings have
an informational severity until reviewed by the agent and remain unverified.

The local journal is under `.local-coder/engagements/`, with atomic owner-only
JSON snapshots and a SHA-256 chain checked on load. This detects accidental
journal modification; it is not a signature or protection against someone who
can rewrite the local files. Reports expose scope, exclusions, coverage, failed
attempts, hypotheses and evidence. GET `/engagements/<id>/report` returns JSON
containing the structured engagement and Markdown report. The manager displays
this under **Engagement evidence & coverage**, and the IDE opens a report document.
Task cancellation and safe checkpoint recovery use the existing harness; the
request budget and evidence survive engine restarts.

CLI examples:

```sh
lc redteam --target src --exclude src/generated 'Map input and authorization boundaries'
lc redteam --origin http://127.0.0.1:8080 --active-http \
  --authorization 'Local disposable lab approval' 'Review application exposure'
lc redteam --engagement eng-0123456789ab 'Disprove the remaining attack-path hypotheses'
```

The methodology is adapted from the MIT-licensed
[S1N6H/pentest-harness pentest preset](https://github.com/S1N6H/pentest-harness/blob/4638f38082bdf37cea03ad151905c4bb8662f6e9/apps/cli/config/agent-presets/pentest/agent.cordis.yml).
This is a native integration in our Python engine, not a bundled second agent host.
The reference commit and preserved license are in `docs/references/`.

## Persistence, compaction and model profiles

A premature task-level surrender triggers up to three different recovery
strategies by default (four attempts including the initial response), within the
existing time/tool/context limits. The cloud provider settings expose a 1–10
strategy budget. Progress observations and used attempts survive checkpoints.
Repeated identical failing calls still trigger a nudge and then a bounded stop;
user cancellation, genuine policy/authorization stops and hard budgets do not
trigger attempts to bypass the restriction. Textual surrender detection is a
conservative heuristic, not a guarantee of task completion.

Compaction archives the full original history in owner-only files, preserves
instructions and whole tool exchanges, and shortens stale tool text. The
`recover_context` tool can search/page only references owned by the current
agent role; it does not expose opaque provider reasoning as tool text. Archives
are integrity checked and capped at 256 MB per workspace. The context estimate
reserves space for the tool catalog and configured output allowance. Character
counts are a conservative estimate, not the provider's exact tokenizer.

OpenRouter presets use the catalog checked on October 25, 2026:
`deepseek/deepseek-v4.1-flash-1017`, `z-ai/glm-5.3` and
`moonshotai/kimi-k3.1-pro`. They use the unified reasoning request and retain the
complete `reasoning_details` sequence through tool follow-ups. Unsupported Kimi
temperature input is omitted. Model-specific profiles improve protocol handling
and recovery; they do not add a safety-bypass jailbreak.

See `docs/references/openrouter-models.json`,
https://openrouter.ai/docs/guides/best-practices/reasoning-tokens and
`docs/harness-research.md`. These adapters are tested with synthetic transports,
not paid provider requests or measured model accuracy benchmarks.

## Review playbooks and proxy artifacts

**Security playbooks** in the manager displays 13 original local review guides.
The agent's `security_playbook` tool selects evidence requirements, source/sink
checks and negative cases. Python static detection reports source traces,
possible callers/guards and exploitation preconditions. It remains bounded,
intraprocedural analysis; JavaScript coverage is explicitly lexical. Scanner
candidates do not establish that a guard is effective or a vulnerability exists.

`redteam_import_findings` also accepts `har` and `burp_xml` for up to 100 scoped
HTTP-history observations under the existing 1 MB import limit. Imports remove
request credentials, all query values and response bodies. XML entities/DTDs are
rejected. `redteam_http_check` with `check: security_txt` can validate bounded
Contact/Expires disclosure metadata on an authorized security.txt URL. No contact
is made and no authorization is inferred from that metadata.

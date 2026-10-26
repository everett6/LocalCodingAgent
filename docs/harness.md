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

Each served workspace has one OS-backed engine lease and allows one running job. A second independent engine is rejected; the desktop manager and IDE share an explicit authenticated connection. Activity and reports are retained
in the local session journal; job failure state survives the live event ring.
Closing an owning host first requests authenticated engine shutdown and drains active jobs. Stop cancels model work and reaps agent-owned command/test/build process trees
before releasing the workspace. Review the diff for partial edits after stopping.

Full-round checkpoints save task routing, messages, tool/test limits and outcomes.
Resume continues the saved agent step; it does not reconstruct an enclosing task DAG. Reconnect the same provider after an engine restart before resuming. Resume uses the latest safe boundary in the selected workspace's state directory.
An incomplete model response or interrupted tool batch is never replayed as a
completed exchange. A checkpoint cannot undo an external side effect; always
inspect partial workspace changes before resuming.

Adapter, process lifecycle and end-to-end tests use synthetic projects and fake
model responses. No paid Anthropic/GLM/Qwen account request is needed to run them.

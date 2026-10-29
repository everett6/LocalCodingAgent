# Task 4 report: runtime bounds and catalog snapshots

Implemented items **10, 11, 12, 18** in the four owned runtime modules and `tests/test_runtime_bounds_50.py`.

## Changes and evidence

| Item | Implementation | Focused evidence |
| --- | --- | --- |
| 10 | `processes.communicate` incrementally drains both pipes in 8 KiB reads and retains bounded byte tails. Default limits are 200,000 bytes per stream and 400,000 combined, including truncation labels. Active pipes split the combined budget equally; single-pipe capture can use its entire budget. Readers continue through EOF after overflow. Timeout/cancellation terminate the existing process tree while shielded drains remain active. Existing tuple results and default timeout are retained; piped stdin receives EOF. UTF-8 tail boundaries remain decodable for existing textual callers. The verification runner sets a 200,000 combined cap and exposes truncation in both raw output and formatted notes. | Real child emits 2 MiB to each pipe while its `proc.communicate` is forbidden; capture stays under 4,096 per stream/6,000 combined and exits successfully. Real continuously writing children are reaped on timeout and cancellation. Real runner executes a million-character unittest output case, preserves its summary, and labels truncation. UTF-8/stdin regression exercises the shared helper. |
| 11 | Verification gives each subprocess a fresh `PYTHONPYCACHEPREFIX` inside its existing temporary report directory, alongside `PYTHONDONTWRITEBYTECODE`. This prevents reads of previously valid timestamp-based `.pyc` files without deleting or changing project caches. | Actual `run_tests` subprocesses for **both pytest and unittest** import a module with existing old bytecode after source changes from `VALUE = 1` to `VALUE = 2`, with identical size and timestamp. Both execute the new value; original bytecode bytes remain unchanged. |
| 12 | Memory history uses bounded per-task deques, a bounded global deque, and an LRU limit on retained task IDs. Defaults: 1,000 events per task, 1,000 retained tasks, 1,000 global events. `get_recent` returns a separate tail list and zero/negative limits return empty lists. Websocket queues default to 256 messages and reject messages exceeding 1,000,000 UTF-8 bytes. Overflow raises explicitly, marks the client failed, closes available socket adapters with code 1013/reconnect reason within a one-second close deadline, and lets the broadcaster remove the client. | Regressions verify newest events, per-task/global/task-count caps, list-copy behavior and limit edges. Websocket tests verify full-queue and oversized-message rejection, explicit socket close, removal of the overflowed subscriber, and continued delivery to a healthy subscriber. Existing callback reentrancy/priority regressions remain green. |
| 18 | Each `CatalogService` retains one normalized snapshot and an indexed model-ID lookup. Reuse checks device, inode, size, nanosecond mtime/ctime, mode and link count. New reads are bounded and compare open-descriptor/path identity before and after reading. TTL/future timestamp checks run on every lookup. Public results deep-copy nested metadata to prevent cache mutation. Existing state-path/privacy checks, network rules, pricing normalization, quota and context decisions remain in place. | Counter regression proves repeated lookups normalize once, then reload on same-size atomic replacement with restored mtime and on an in-place rewrite with restored mtime. Tests also cover TTL expiration without file changes, nested mutation isolation, malformed replacement, restored valid file, and deletion. Existing catalog, provider API, quota and efficiency regressions pass. |

## Verification

Before implementation, the original eight focused regressions all failed, including the actual stale-bytecode runner failure (`1 != 2`), missing truncation label, absent bounds and repeated catalog normalization. The focused file now contains eleven passing cases including parametrized variants.

Final command:

```bash
PYTHONPATH=src:/tmp/local-coder-merge-agent-deps python -m pytest -q \
  tests/test_runtime_bounds_50.py tests/test_event_bus_regressions.py \
  tests/test_process_cancellation.py tests/test_provider_catalog.py \
  tests/test_test_runner.py tests/test_provider_efficiency_regressions.py \
  tests/test_provider_api.py tests/test_provider_quota.py \
  tests/test_security_tools.py tests/test_security_tools_and_agent.py
```

Result: **140 passed, 1 skipped, 6 pre-existing `asyncio.iscoroutinefunction` deprecation warnings**, in 6.58 seconds. The skip is the existing Go runner integration test because Go is not installed. `git diff --check` for owned changes passed. Ruff is unavailable; no tools were installed.

An earlier broader check encountered a temporary shared-worktree integration failure from `ToolName.TOOL_CATALOG` not yet being present; it was communicated to the parent and is resolved in the final run. No cross-owned files were changed to resolve it.

## Limits and remaining gates

- Parent review, integration, and aggregate verification remain parent-owned. No full suite, commits, pushes, installs, provider calls or downloads were performed.
- Windows process-tree behavior retains the existing taskkill implementation and was not exercised on this Linux host; the existing Go integration skip remains external.
- The combined capture budget is deliberately divided across active pipes rather than dynamically reallocated. Structured stdout parsers receive a labeled bounded tail; reports larger than the cap can omit early details, while process exit status remains authoritative.
- Histories are bounded replay tails, not durable event archives. Queue overflow requires reconnect/resync rather than silently losing events while keeping the subscription live. Memory history bounds are event/task counts; each individual event retains its existing payload contract.
- File identity relies on OS inode/device and nanosecond timestamp/change metadata, with descriptor/path consistency checks for reloads. Quota/account data is still requested live and is never stored in this catalog snapshot.

"""Conservative tool effects, bounded read batches and durable call outcomes."""
from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ToolEffects:
    read_only: bool = False
    mutating: bool = True
    retry_safe: bool = False

    @property
    def concurrent(self):
        return self.read_only and not self.mutating and self.retry_safe


# Trust concrete engine implementations, never a model name or agent role.
_BUILTIN_READS = {
    "local_coder.tools.filesystem": {"ReadFileTool", "ListFilesTool"},
    "local_coder.tools.search": {"SearchFilesTool", "GrepTool"},
    "local_coder.tools.git": {"GitStatusTool", "GitDiffTool", "GitLogTool"},
    "local_coder.tools.code_search": {"CodeSearchTool"},
    "local_coder.tools.repo_map": {"RepoMapTool"},
}


def tool_effects(tool):
    if tool is None:
        return ToolEffects()
    cls = type(tool)
    if cls.__name__ in _BUILTIN_READS.get(cls.__module__, set()):
        module = sys.modules.get(cls.__module__)
        if module is not None and getattr(module, cls.__name__, None) is cls:
            return ToolEffects(True, False, True)
    names = ("read_only", "mutating", "retry_safe")
    if not all(name in cls.__dict__ or name in getattr(tool, "__dict__", {}) for name in names):
        return ToolEffects()
    values = [getattr(tool, name, None) for name in ("read_only", "mutating", "retry_safe")]
    if any(type(value) is not bool for value in values) or not (values[0] or values[1]):
        return ToolEffects()
    return ToolEffects(*values)


class ToolBatchJournal:
    """Extend the runtime's pending marker; arguments and output stay out of it.

    A complete checkpoint removes this marker. Until then even completed calls
    are not replayable from an older checkpoint; a started mutation is unknown.
    """
    def __init__(self, run, calls, effects):
        self.run = run
        self.lock = asyncio.Lock()
        self.document = {"iteration": run.state.iteration if run else 0,
                         "tools": [{"id": tc.id, "name": tc.name,
                                    "effects": asdict(effects(tc)), "status": "pending",
                                    "mutation_unknown": False} for tc in calls]}

    async def mark(self, call, status, *, unknown=False, outcome=None):
        async with self.lock:
            row = next(item for item in self.document["tools"] if item["id"] == call.id)
            row["status"] = status
            row["mutation_unknown"] = unknown
            if outcome is not None:
                row["outcome"] = outcome
            if self.run is not None:
                from local_coder.core.runtime import _write_json
                # Shield and await the writer even during cancellation: do not
                # leave a background writer racing a newer completion record.
                writer = asyncio.create_task(asyncio.to_thread(_write_json, self.run.pending_batch_path, self.document))
                try:
                    await asyncio.shield(writer)
                except asyncio.CancelledError:
                    await writer
                    raise


async def scheduled_calls(calls, execute, effects, *, concurrency=4):
    """Yield original call/result order, with barriers at every unsafe call.

    Only adjacent declared reads overlap. Exceptions cancel and await all other
    in-flight reads before propagating. No tool is ever retried here.
    """
    if type(concurrency) is not int or not 1 <= concurrency <= 4:
        raise ValueError("Tool concurrency must be between one and four")
    index = 0
    while index < len(calls):
        if not effects(calls[index]).concurrent:
            call = calls[index]
            yield call, await execute(call)
            index += 1
            continue
        end = index
        while end < len(calls) and end - index < concurrency and effects(calls[end]).concurrent:
            end += 1
        tasks = [asyncio.create_task(execute(call)) for call in calls[index:end]]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for call, result in zip(calls[index:end], results):
            yield call, result
        index = end

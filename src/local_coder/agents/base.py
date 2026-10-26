"""Base agent with tool-calling loop."""
from __future__ import annotations
import asyncio
import json
import logging
import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from local_coder.types import (
    AgentRole, AgentTask, AgentResponse, AgentState, AgentPhase, TaskStatus,
    Message, ToolResult, ModelResponse, AgentMetrics, AgentEvent, TestResult, ToolName,
)
from local_coder.core.runtime import AgentRun
from local_coder.context.compression import (
    SUMMARY_SYSTEM_PROMPT,
    compress_messages,
    message_size,
    prune_tool_outputs,
    summarize_history,
    truncate_tool_output,
)
from local_coder.statedir import state_path
from local_coder.agents.tool_repair import (
    RepairedCall, extract_text_tool_calls, repair_tool_call, schemas_by_name,
)
from local_coder.verification.syntax import check_syntax
from local_coder.workspace import Workspace

logger = logging.getLogger(__name__)

PINNED_PREFIX = "Pinned notes (kept verbatim through context compaction):\n"


class BaseAgent:
    """Base agent with model interaction and tool-calling loop."""

    role: AgentRole
    system_prompt: str
    max_iterations: int = 15  # Max tool-calling rounds
    max_tool_calls: int = 100
    max_test_runs: int = 20
    # Stagnation guard: a small local model is much likelier than a frontier
    # one to retry an identical failing tool call instead of changing
    # approach. After this many IDENTICAL (name + arguments) failing calls in
    # a row, inject a corrective nudge; after this many more, give up rather
    # than silently burning the rest of the iteration budget on a call that
    # has never once succeeded.
    stagnation_warning_threshold: int = 3
    stagnation_abort_threshold: int = 5
    # A quantized local model occasionally emits a tool call whose
    # arguments aren't valid JSON (single quotes, a truncated brace) --
    # llama-server's parser rejects that with a 500 rather than truncating
    # or repairing it. That's usually just a bad sample, not a structural
    # failure, so retry generation this many extra times (with a short
    # backoff) before giving up on the whole task over one glitch.
    strategy_attempt_limit: int = 3
    generation_retry_limit: int = 2
    generation_retry_backoff_seconds: float = 0.5
    # Small-model guardrails (see tool_repair.py and verification/syntax.py):
    # accept tool calls written as text when no native call was made, refuse
    # to let write_file clobber an existing file the agent never read, and
    # parse-check every file an edit touches so a syntax error surfaces in
    # the same turn instead of in a test run several turns later.
    parse_text_tool_calls: bool = True
    require_read_before_overwrite: bool = True
    check_syntax_after_edits: bool = True
    # Context budget. A single tool result is capped to a slice of the
    # window (the rest is saved to disk, see truncate_tool_output), and the
    # conversation is compacted once the server reports the prompt within
    # this fraction of its real token limit, even if the character budget
    # still looks fine -- chars/token varies a lot between code and prose.
    max_tool_output_lines: int = 400
    context_token_threshold: float = 0.85
    max_summary_tokens: int = 1024
    tool_output_dir = ".local-coder/tool-output"

    def __init__(
        self,
        model,  # LocalModel protocol
        tool_registry,  # ToolRegistry
        event_callback: Callable[[AgentEvent], None] | None = None,
        context_window_chars: int = 24000,
        compact_context_chars: int = 12000,
        drafter=None,  # Optional[SpeculativeDrafter]
        max_tool_output_chars: int | None = None,
    ):
        self.model = model
        self.strategy_attempt_limit = getattr(getattr(model, "config", None), "strategy_attempt_limit", self.strategy_attempt_limit)
        self.tool_registry = tool_registry
        self._event_callback = event_callback
        self._metrics = AgentMetrics()
        # Keep track of files changed during this agent's execution
        self._files_changed: set[str] = set()
        self._tests_run: list[str] = []
        self._tests_passed = True
        self._test_outcomes = {}
        self.state: AgentState | None = None
        self.context_window_chars = context_window_chars
        self.compact_context_chars = compact_context_chars
        self._last_failing_call_signature: str | None = None
        self._repeated_failure_count: int = 0
        self._stagnation_warned: bool = False
        self.drafter = drafter
        # Registries without a runtime remain usable by embedders and unit tests.
        self.runtime = getattr(tool_registry, "runtime", None)
        self._run: AgentRun | None = None
        self._strategy_attempts = 0
        self._progress_journal: list[dict] = []
        self._context_archives: dict[str, str] = {}
        self._archive_references: list[str] = []
        self.max_tool_output_chars = max_tool_output_chars or max(2000, min(16000, context_window_chars // 8))
        self._last_prompt_tokens = 0
        self._summary_calls = 0

    async def execute(self, task: AgentTask) -> AgentResponse:
        """Execute a task through the tool-calling loop, optionally primed
        with a speculative draft of the first relevant file's edit."""
        draft_prediction = None
        if self.drafter is not None and task.files:
            draft_prediction = await self._maybe_draft(task)

        try:
            response = await self._execute_loop(task, draft_prediction)
            if draft_prediction is not None:
                self._settle_draft(draft_prediction, response)
            await self._finish_run(response)
            return response
        except BaseException:
            await self._release_interrupted_run()
            raise

    async def resume(self, run: AgentRun) -> AgentResponse:
        """Continue a recovered complete round without rerunning preparation/tools."""
        if run.task.role != self.role:
            raise ValueError("Checkpoint agent role does not match")
        run.validate_resume()
        await run.validate_pending_batch()
        self._run = run
        self.runtime = run._runtime
        self.state = run.state
        self._metrics = run.metrics.model_copy(deep=True)
        self._files_changed = set(run.files_changed)
        self._tests_run = [result.test_name for result in self.state.test_results]
        self._test_outcomes = {result.test_name: result.passed for result in self.state.test_results}
        self._tests_passed = all(self._test_outcomes.values())
        self.max_iterations = self.state.max_iterations
        self.max_tool_calls = self.state.max_tool_calls
        self.max_test_runs = self.state.max_test_runs
        self.context_window_chars = run.notes.get("context_window_chars", self.context_window_chars)
        self.compact_context_chars = run.notes.get("compact_context_chars", self.compact_context_chars)
        self._last_failing_call_signature = run.notes.get("last_failing_call_signature")
        self._repeated_failure_count = run.notes.get("repeated_failure_count", 0)
        self._stagnation_warned = run.notes.get("stagnation_warned", False)
        self._strategy_attempts = run.notes.get("strategy_attempts", 0)
        self._progress_journal = run.notes.get("progress_journal", [])
        self.strategy_attempt_limit = run.notes.get("strategy_attempt_limit", self.strategy_attempt_limit)
        self._summary_calls = run.notes.get("summary_calls", 0)
        self._last_prompt_tokens = run.notes.get("last_prompt_tokens", 0)
        self.max_tool_output_chars = run.notes.get("max_tool_output_chars", self.max_tool_output_chars)
        self.max_tool_output_lines = run.notes.get("max_tool_output_lines", self.max_tool_output_lines)
        self._archive_references = run.notes.get("context_archive_refs", [])
        for reference in self._archive_references:
            self._register_archive(reference)
        try:
            response = await self._execute_loop(run.task, None, resumed=True)
            await self._finish_run(response)
            return response
        except BaseException:
            await self._release_interrupted_run()
            raise

    async def _release_interrupted_run(self) -> None:
        if self._run is not None:
            await self.runtime.release_run(self._run.run_id)
            self._run = None

    async def _checkpoint_boundary(self) -> None:
        if self._run is None:
            return
        notes = {
            "boundary_complete": True,
            "strategy_attempts": self._strategy_attempts,
            "strategy_attempt_limit": self.strategy_attempt_limit,
            "progress_journal": self._progress_journal[-20:],
            "context_archive_refs": self._archive_references,
            "context_window_chars": self.context_window_chars,
            "compact_context_chars": self.compact_context_chars,
            "last_failing_call_signature": self._last_failing_call_signature,
            "repeated_failure_count": self._repeated_failure_count,
            "stagnation_warned": self._stagnation_warned,
            "last_prompt_tokens": self._last_prompt_tokens,
            "summary_calls": self._summary_calls,
            "max_tool_output_chars": self.max_tool_output_chars,
            "max_tool_output_lines": self.max_tool_output_lines,
        }
        permissions = getattr(self.tool_registry, "_permissions", {}).get(self.role)
        if permissions is not None:
            notes["allowed_tools"] = sorted(name.value for name in permissions)
        path = await self._run.checkpoint_boundary(self.state, self._metrics, notes)
        self._emit_event("checkpoint_saved", "Saved complete agent round", task_id=self.state.task_id,
                         data={"path": path})

    async def _finish_run(self, response: AgentResponse) -> None:
        self.state.phase = AgentPhase.DONE if response.status == TaskStatus.COMPLETED else AgentPhase.FAILED
        self.state.finished_at = datetime.now()
        if self._run is not None:
            await self.runtime.end_run(self._run.run_id, self.state,
                                       success=response.status == TaskStatus.COMPLETED, result=response)
            self._run = None

    async def _maybe_draft(self, task: AgentTask):
        """Best-effort speculative draft of the first listed file's edit,
        using the same tool the model would use to read it. Never raises --
        a failure here just means no draft, not a broken task."""
        file_path = task.files[0]
        try:
            result = await self.tool_registry.execute_tool(self.role, "read_file", {"path": file_path})
            if not result.success:
                return None
            return await self.drafter.predict_edit(file_path, result.output, task.objective)
        except Exception:
            return None

    def _settle_draft(self, draft_prediction, response: AgentResponse) -> None:
        """Feed back whether the draft actually matched what the agent did,
        so PredictionPolicy's should_predict() adapts over time instead of
        drafting forever regardless of whether it ever helps."""
        useful = (
            response.status == TaskStatus.COMPLETED
            and draft_prediction.file_path in response.files_changed
        )
        if useful:
            self.drafter.accept_prediction(draft_prediction.prediction_id)
        else:
            self.drafter.reject_prediction(draft_prediction.prediction_id)

    async def _execute_loop(self, task: AgentTask, draft_prediction, resumed=False) -> AgentResponse:
        # Build initial messages
        messages = list(self.state.messages) if resumed else self._build_messages(task)
        if draft_prediction is not None:
            messages.append(Message(
                role="system",
                content=(
                    f"Speculative draft for {draft_prediction.file_path} (unverified -- a "
                    "fast draft model's guess, not applied to any file). Use it as a "
                    "starting point if it looks right; verify and correct it before relying "
                    f"on it, don't apply it blindly:\n{draft_prediction.content}"
                ),
            ))
        if not resumed:
            self._files_changed.clear()
            self._metrics = AgentMetrics()
            self._tests_run = []
            self._tests_passed = True
            self._test_outcomes = {}
            self._last_failing_call_signature = None
            self._repeated_failure_count = 0
            self._stagnation_warned = False
            self._run = None
            self._strategy_attempts = 0
            self._progress_journal = []
            self._last_prompt_tokens = 0
            self._summary_calls = 0
            self.state = AgentState(
                task_id=task.task_id,
                objective=task.objective,
                max_iterations=self.max_iterations,
                max_tool_calls=self.max_tool_calls,
                max_test_runs=self.max_test_runs,
                started_at=datetime.now(),
            )
            self.state.phase = AgentPhase.EXECUTING
            self.state.messages = list(messages)
            if self.runtime is not None:
                # Store the configured model key, so changed role routing cannot
                # silently select another model on restart.
                selector = getattr(self.runtime, "model_name_for", None)
                if task.model_name is None and selector is not None:
                    task = task.model_copy(update={"model_name": selector(self.model)})
                self._run = await self.runtime.start_run(task, self.state)
                await self._checkpoint_boundary()
        
        message_budget = self._message_budget()
        for iteration in range(self.state.iteration, self.max_iterations):
            if self._needs_compaction(messages):
                messages = self._repin(await self._compact(messages, task))
                self.state.messages = list(messages)
            if sum(message_size(message) for message in messages) > message_budget:
                self.state.phase = AgentPhase.FAILED
                return self._build_response(task, ModelResponse(content="Context budget exceeded. Narrow the investigation scope or increase the configured context limit."),
                                            TaskStatus.FAILED, issues=["Context budget exceeded"])
            self.state.iteration = iteration + 1
            self._emit_event(
                "iteration_started",
                f"Starting tool loop iteration {iteration + 1}/{self.max_iterations}",
                task_id=task.task_id,
            )

            base_temperature = self.model.config.temperature if hasattr(self.model, "config") else 0.2
            tool_schemas = self.tool_registry.get_schemas_for_role(self.role)
            response = None
            generation_error: Exception | None = None
            for attempt in range(self.generation_retry_limit + 1):
                try:
                    # A malformed-tool-call-JSON failure from a quantized
                    # model can be a near-deterministic mode of the
                    # distribution at this exact prompt, not just sampling
                    # noise -- retrying with the same temperature reproduces
                    # it every time. Nudge temperature up a bit each retry
                    # to actually diversify the sample instead of repeating
                    # the same bad draw, capped so it doesn't get incoherent.
                    retry_temperature = min(base_temperature + 0.15 * attempt, 1.0)
                    response = await self.model.generate(
                        list(messages),
                        temperature=retry_temperature,
                        max_tokens=self.model.config.max_tokens if hasattr(self.model, "config") else 4096,
                        tools=tool_schemas,
                    )
                    generation_error = None
                    break
                except Exception as exc:
                    generation_error = exc
                    if attempt < self.generation_retry_limit:
                        self._emit_event(
                            "model_retry",
                            f"Generation failed ({exc}); retrying ({attempt + 1}/{self.generation_retry_limit})",
                            task_id=task.task_id,
                        )
                        await asyncio.sleep(self.generation_retry_backoff_seconds)

            if generation_error is not None:
                total_attempts = self.generation_retry_limit + 1
                self.state.phase = AgentPhase.FAILED
                self.state.errors.append(
                    f"Model generation failed after {total_attempts} attempts: {generation_error}"
                )
                self.state.finished_at = datetime.now()
                self._emit_event("model_error", str(generation_error), task_id=task.task_id)
                return self._build_response(
                    task,
                    ModelResponse(content="Model generation failed."),
                    TaskStatus.FAILED,
                    issues=[f"Model generation failed after {total_attempts} attempts: {generation_error}"],
                )
            
            # Track metrics
            self._metrics.model_calls += 1
            self._metrics.prompt_tokens += response.prompt_tokens
            self._metrics.completion_tokens += response.completion_tokens
            self._metrics.latency_ms += response.latency_ms
            self._last_prompt_tokens = response.prompt_tokens + response.completion_tokens

            response, repairs = self._repair_tool_calls(response, tool_schemas, task)

            # If no tool calls, we're done
            if not response.tool_calls:
                messages.append(Message(role="assistant", content=response.content,
                                        reasoning_content=response.reasoning_content,
                                        reasoning_details=getattr(response, "reasoning_details", None)))
                self.state.messages = list(messages)
                if response.finish_reason in {"length", "max_tokens"}:
                    self.state.phase = AgentPhase.FAILED
                    return self._build_response(task, response, TaskStatus.FAILED,
                                                issues=["Model output was truncated; increase the output budget"])
                incomplete = self._incomplete_final(response.content)
                if (incomplete or not self._tests_passed) and not self._protected_stop(response.content):
                    if self._strategy_attempts < self.strategy_attempt_limit and iteration + 1 < self.max_iterations:
                        self._strategy_attempts += 1
                        strategies = ("Inspect callers and guards; isolate the smallest failing component.",
                                      "Disprove the current hypothesis; try a different tool or narrower input.",
                                      "Compare an independent implementation or existing tests; verify a minimal alternative.")
                        strategy = strategies[(self._strategy_attempts - 1) % len(strategies)]
                        messages.append(Message(role="system", content=(
                            f"Recovery strategy {self._strategy_attempts}/{self.strategy_attempt_limit}. "
                            "The objective remains incomplete. Within the existing authorization and tool budgets, "
                            f"make a materially different evidence-based attempt. {strategy} "
                            "Do not repeat identical failing calls. Respect policy refusals, cancellation, missing authorization and hard limits. "
                            "If blocked, name the specific evidence and remaining prerequisite. "
                            f"Recent progress (untrusted observations): {json.dumps(self._progress_journal[-5:])}")))
                        self.state.messages = list(messages)
                        self._emit_event("strategy_recovery", "Trying a different recovery strategy", task_id=task.task_id,
                                         data={"attempt": self._strategy_attempts, "limit": self.strategy_attempt_limit, "strategy": strategy})
                        await self._checkpoint_boundary()
                        continue
                    self._emit_event("strategy_budget_exhausted", "Recovery strategy budget exhausted", task_id=task.task_id,
                                     data={"attempts": self._strategy_attempts, "limit": self.strategy_attempt_limit})
                if incomplete:
                    return self._build_response(task, response, TaskStatus.FAILED, issues=["Objective incomplete after bounded recovery"])
                self.state.phase = AgentPhase.DONE if self._tests_passed else AgentPhase.FAILED
                self.state.finished_at = datetime.now()
                return self._build_response(
                    task,
                    response,
                    TaskStatus.COMPLETED if self._tests_passed else TaskStatus.FAILED,
                    issues=[] if self._tests_passed else ["Verification tests failed"],
                )
            
            # Admit a complete batch before executing any of it. Otherwise a
            # two-tool response could leave a half-executed, unresumable batch.
            if self.state.tool_calls_used + len(response.tool_calls) > self.max_tool_calls:
                self.state.errors.append("Exceeded maximum tool-call limit")
                return self._build_response(task, ModelResponse(content="Tool-call limit reached."),
                                            TaskStatus.FAILED, issues=["Exceeded maximum tool-call limit"])
            requested_tests = sum(tc.name == "run_tests" for tc in response.tool_calls)
            if self.state.test_runs + requested_tests > self.max_test_runs:
                self.state.errors.append("Exceeded maximum test-run limit")
                return self._build_response(task, ModelResponse(content="Test-run limit reached."),
                                            TaskStatus.FAILED, issues=["Exceeded maximum test-run limit"])
            # Add assistant message with tool calls
            messages.append(Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
                reasoning_content=response.reasoning_content,
                reasoning_details=getattr(response, "reasoning_details", None),
            ))
            self.state.messages = list(messages)
            self.state.tool_calls.extend(response.tool_calls)
            if self._run is not None:
                await self._run.begin_tool_batch(response.tool_calls)
            
            # Execute tool calls
            for tc in response.tool_calls:
                self._emit_event(
                    "tool_call",
                    f"Calling {tc.name}({json.dumps(tc.arguments)[:100]})",
                    task_id=task.task_id,
                )
                
                repair = repairs.get(tc.id)
                blocked = repair.error if repair is not None else None
                if blocked is None:
                    blocked = self._check_overwrite(tc, task)
                if blocked is not None:
                    result = ToolResult(tool_call_id=tc.id, success=False, output=blocked)
                else:
                    try:
                        result = await self.tool_registry.execute_tool(
                            self.role, tc.name, tc.arguments
                        )
                    except Exception as exc:
                        result = ToolResult(
                            tool_call_id=tc.id,
                            success=False,
                            output=f"Tool execution failed: {exc}",
                        )
                    if result.success and result.files_changed:
                        self._append_syntax_errors(result, task)
                if repair is not None and repair.notes:
                    result.output = f"[Note: {'; '.join(repair.notes)}]\n{result.output}"

                self._progress_journal.append({"tool": tc.name, "success": result.success,
                                               "arguments": json.dumps(tc.arguments, default=str)[:400], "observation": (result.error or result.output or "")[:240]})
                self._progress_journal = self._progress_journal[-20:]
                self._metrics.tool_calls += 1
                self.state.tool_calls_used += 1
                if tc.name in {"read_file", "search_files", "grep"}:
                    path = tc.arguments.get("path")
                    if path:
                        self.state.files_read.add(path)
                        self._metrics.files_read = len(self.state.files_read)
                if result.files_changed:
                    self._metrics.files_written += len(result.files_changed)
                    self._files_changed.update(result.files_changed)
                    self.state.files_changed.update(result.files_changed)
                if tc.name == "run_tests":
                    self.state.phase = AgentPhase.VERIFYING
                    self.state.test_runs += 1
                    test_name = tc.arguments.get("target") or tc.arguments.get("test_path") or "project tests"
                    self._tests_run.append(test_name)
                    self._test_outcomes[test_name] = result.success
                    self._tests_passed = all(self._test_outcomes.values())
                    if not result.success:
                        self.state.phase = AgentPhase.REFLECTING
                    self.state.test_results.append(TestResult(
                        test_name=tc.arguments.get("target") or tc.arguments.get("test_path") or "project tests",
                        passed=result.success,
                        duration_ms=result.duration_ms or 0.0,
                        error_message=None if result.success else result.output,
                        stdout=result.output,
                    ))
                if not result.success:
                    self.state.errors.append(result.error or result.output)

                # Stagnation tracking: has this exact (tool, arguments) call
                # just failed again, identically to the immediately preceding
                # call? Anything else -- a different call, or this one
                # finally succeeding -- resets the streak.
                call_signature = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True, default=str)}"
                if not result.success and call_signature == self._last_failing_call_signature:
                    self._repeated_failure_count += 1
                elif not result.success:
                    self._repeated_failure_count = 1
                    self._stagnation_warned = False
                else:
                    self._repeated_failure_count = 0
                    self._stagnation_warned = False
                self._last_failing_call_signature = call_signature if not result.success else None

                # Add tool result as message
                tool_output = result.output
                if not result.success and result.error:
                    tool_output = f"{tool_output}\nError: {result.error}".strip()

                messages.append(Message(
                    role="tool",
                    content=self._fit_tool_output(tc, tool_output or "Tool completed without output."),
                    tool_call_id=tc.id,
                    name=tc.name,
                ))
                self.state.messages = list(messages)
                self._emit_event(
                    "tool_result",
                    f"{tc.name}: {'succeeded' if result.success else 'failed'}",
                    task_id=task.task_id,
                )

                if self._repeated_failure_count >= self.stagnation_abort_threshold:
                    self.state.phase = AgentPhase.FAILED
                    self.state.errors.append(
                        f"Aborted: {tc.name} failed identically {self._repeated_failure_count} times in a row"
                    )
                    self.state.finished_at = datetime.now()
                    self._emit_event(
                        "stagnation_abort",
                        f"Giving up after {self._repeated_failure_count} identical failing calls to {tc.name}",
                        task_id=task.task_id,
                    )
                    return self._build_response(
                        task,
                        ModelResponse(content=f"Stuck repeating a failing {tc.name} call; aborting."),
                        TaskStatus.FAILED,
                        issues=[
                            f"Repeated the same failing {tc.name} call "
                            f"{self._repeated_failure_count} times without making progress"
                        ],
                    )
                if self._repeated_failure_count >= self.stagnation_warning_threshold and not self._stagnation_warned:
                    self._stagnation_warned = True
                    messages.append(Message(
                        role="system",
                        content=(
                            f"You have called {tc.name} with the exact same arguments "
                            f"{self._repeated_failure_count} times in a row and it keeps failing the "
                            "same way. Repeating it again will not help. Read the error carefully, "
                            "then either fix the underlying cause, use different arguments, or try a "
                            "different tool entirely."
                        ),
                    ))
                    self.state.messages = list(messages)
                    self._emit_event(
                        "stagnation_warning",
                        f"Nudged the model after {self._repeated_failure_count} identical failing calls to {tc.name}",
                        task_id=task.task_id,
                    )
            # Never save between tool results in the same assistant response.
            await self._checkpoint_boundary()
        
        # Max iterations reached
        self.state.phase = AgentPhase.FAILED
        self.state.errors.append("Exceeded maximum tool-calling iterations")
        self.state.finished_at = datetime.now()
        return self._build_response(
            task,
            ModelResponse(content="Max iterations reached"),
            TaskStatus.FAILED,
            issues=["Exceeded maximum tool-calling iterations"],
        )
    
    @staticmethod
    def _incomplete_final(content: str) -> bool:
        return bool(re.search(r"\b(?:i (?:cannot|can't|could not|couldn't|am unable to)|unable to (?:complete|fix|solve|implement)|giving up on (?:the )?(?:task|objective)|(?:task is |this is |it's )?too (?:difficult|complex|hard)(?: to complete)?|beyond my capabilities|task (?:failed|incomplete)|could not (?:complete|fix|solve))\b", content, re.I))

    @staticmethod
    def _protected_stop(content: str) -> bool:
        return bool(re.search(r"\b(?:not authori[sz]ed|authorization (?:is )?(?:missing|required|not provided)|(?:missing|no) authorization|permission (?:is )?required|user cancel|cancelled|canceled|out of scope|not permitted|resource limit|budget exhausted|(?:safety|security|usage) policy|(?:against|violates) (?:my |the )?policy|(?:harmful|malicious|illegal) request|(?:cannot|can't) (?:help|assist|comply))\b", content, re.I))

    def _register_archive(self, reference, payload=None):
        if not hasattr(self.tool_registry, "context_archives"):
            self.tool_registry.context_archives = {}
        self.tool_registry.context_archives[reference] = {"role": self.role.value, "payload": payload}

    def _archive_context(self, messages: list[Message]) -> str:
        payload = json.dumps([m.model_dump(mode="json") for m in messages], ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        if self.runtime is not None:
            directory = Path(self.runtime.state_dir) / "context-archives"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.resolve().parent != Path(self.runtime.state_dir).resolve():
                raise ValueError("Context archive directory escapes state directory")
            path = directory / f"{digest}.json"
            if path.is_symlink():
                raise ValueError("Context archive cannot be a symbolic link")
            if not path.exists():
                if sum(p.stat().st_size for p in directory.glob('*.json') if not p.is_symlink()) + len(payload.encode()) > 256_000_000:
                    raise ValueError("Context archive storage budget reached; export and clean older sessions")
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(payload)
            reference = str(path)
            self._register_archive(reference)
            if reference not in self._archive_references:
                self._archive_references.append(reference)
            return reference
        self._context_archives[digest] = payload
        reference = f"agent-memory:{digest}"
        self._register_archive(reference, payload)
        return reference
    def _pinned_context(self) -> str:
        """Notes that must survive context compaction word for word. Empty
        by default; a role that keeps structured state outside the
        conversation (the security reviewer's findings ledger) overrides it."""
        return ""

    def _repin(self, messages: list[Message]) -> list[Message]:
        """After compaction, put the current pinned notes back right after
        the task preamble, replacing any older copy, so a lossy summary can't
        drop them."""
        pinned = self._pinned_context()
        messages = [m for m in messages if not m.content.startswith(PINNED_PREFIX)]
        if not pinned:
            return messages
        head_end = next((i for i, m in enumerate(messages) if m.role in {"assistant", "tool"}), len(messages))
        note = Message(role="user", content=PINNED_PREFIX + pinned)
        return messages[:head_end] + [note] + messages[head_end:]

    def _repair_tool_calls(
        self, response: ModelResponse, tool_schemas: list[dict], task: AgentTask
    ) -> tuple[ModelResponse, dict[str, RepairedCall]]:
        """Recover text-only tool calls and fix near-miss names/arguments."""
        schemas = schemas_by_name(tool_schemas)
        tool_calls = list(response.tool_calls)
        if not tool_calls and self.parse_text_tool_calls and schemas:
            tool_calls = extract_text_tool_calls(response.content, set(schemas))
            if tool_calls:
                self._emit_event(
                    "tool_calls_from_text",
                    f"Recovered {len(tool_calls)} tool call(s) written as text",
                    task_id=task.task_id,
                )
        if not tool_calls:
            return response, {}
        repairs = {tc.id: repair_tool_call(tc, schemas) for tc in tool_calls}
        repaired_calls = [repairs[tc.id].call for tc in tool_calls]
        if repaired_calls != response.tool_calls:
            response = response.model_copy(update={"tool_calls": repaired_calls})
        return response, repairs

    def _workspace(self) -> Workspace | None:
        """The workspace this agent's file tools are rooted at, if known."""
        get_tool = getattr(self.tool_registry, "get_tool", None)
        if get_tool is None:
            return None
        for name in (ToolName.WRITE_FILE, ToolName.READ_FILE):
            try:
                root = getattr(get_tool(name), "project_root", None)
            except Exception:
                root = None
            if root:
                return Workspace(root)
        return None

    def _check_overwrite(self, tc, task: AgentTask) -> str | None:
        """Refuse a write_file that would replace an existing file the agent
        hasn't seen in this task: a small model rewriting a file from memory
        silently drops whatever it didn't remember."""
        if not self.require_read_before_overwrite or tc.name != ToolName.WRITE_FILE.value:
            return None
        path = tc.arguments.get("path")
        workspace = self._workspace()
        if not isinstance(path, str) or workspace is None:
            return None
        try:
            target = workspace.resolve(path)
        except (ValueError, OSError):
            return None  # write_file reports the bad path itself
        if not target.is_file():
            return None
        seen: set[Path] = set()
        known = set(self._files_changed) | set(task.context.file_contents)
        if self.state is not None:
            known |= self.state.files_read
        for candidate in known:
            try:
                seen.add(workspace.resolve(candidate))
            except (ValueError, OSError):
                continue
        if target in seen:
            return None
        return (
            f"Refusing to overwrite {path}: it already exists and you have not read it in this task. "
            "Call read_file on it first, then make the change -- prefer a targeted edit over "
            "rewriting the whole file."
        )

    def _append_syntax_errors(self, result: ToolResult, task: AgentTask) -> None:
        """Add a parse error for any edited file that no longer parses."""
        if not self.check_syntax_after_edits:
            return
        workspace = self._workspace()
        if workspace is None:
            return
        problems = []
        for changed in result.files_changed:
            try:
                problem = check_syntax(workspace.resolve(changed), display_path=changed)
            except (ValueError, OSError):
                continue
            if problem:
                problems.append(problem)
        if not problems:
            return
        result.output = (
            f"{result.output}\n\nWarning: the edit was saved but the file no longer parses:\n"
            + "\n\n".join(problems)
            + "\nFix this before doing anything else."
        ).strip()
        self._emit_event(
            "syntax_error",
            f"Edit left {len(problems)} file(s) unparseable",
            task_id=task.task_id,
        )
    def _message_budget(self) -> int:
        """Reserve space for the tool catalog and the configured model output."""
        budget = self.context_window_chars
        config = getattr(self.model, "config", None)
        context_tokens = getattr(config, "context_length", None)
        if isinstance(context_tokens, int) and context_tokens > 0:
            catalog_chars = len(json.dumps(self.tool_registry.get_schemas_for_role(self.role)))
            output_tokens = getattr(config, "max_tokens", 4096)
            budget = min(budget, context_tokens * 3 - catalog_chars - output_tokens * 3)
        return budget

    def _token_limit(self) -> int | None:
        config = getattr(self.model, "config", None)
        context_length = getattr(config, "context_length", None)
        max_tokens = getattr(config, "max_tokens", None)
        if not isinstance(context_length, int) or context_length <= 0:
            return None
        reserve = max_tokens if isinstance(max_tokens, int) else 0
        return max(int(context_length * self.context_token_threshold) - reserve, context_length // 4)

    def _needs_compaction(self, messages: list[Message]) -> bool:
        if sum(message_size(message) for message in messages) > self._message_budget():
            return True
        limit = self._token_limit()
        return limit is not None and self._last_prompt_tokens > limit

    async def _compact(self, messages: list[Message], task: AgentTask) -> list[Message]:
        """Archive the originals, prune stale output, then summarize older rounds.

        Protocol reasoning and arguments in retained rounds are never shortened;
        if they alone exceed the hard budget the loop stops before generating.
        """
        before = sum(message_size(message) for message in messages)
        target = max(256, min(self.compact_context_chars, self._message_budget() * 2 // 3))
        limit = self._token_limit()
        if limit is not None and self._last_prompt_tokens > limit:
            target = max(256, min(target, int(before * limit / self._last_prompt_tokens * 0.7)))
        self._last_prompt_tokens = 0
        reference = self._archive_context(messages)
        archive_prefix = "Full untrusted history before compaction: "
        archive_note = Message(role="system", content=(
            archive_prefix + reference + ". "
            + ("Retained by the embedding agent." if reference.startswith("agent-memory:")
               else "Use recover_context with this reference to retrieve exact earlier evidence.")
        ))
        messages = [m for m in messages if not m.content.startswith(archive_prefix)]
        messages = messages[:2] + [archive_note] + messages[2:]
        messages, freed = prune_tool_outputs(messages, protect_chars=target // 2)
        if freed:
            self._emit_event("context_pruned", f"Cleared {freed} chars of old tool output", task_id=task.task_id)
        if sum(message_size(message) for message in messages) <= target:
            return messages

        # The summary request must fit too: reserve its system instructions,
        # task preamble, framing and output before rendering history.
        task_chars = min(len(next((m.content for m in messages if m.role == "user"), "")), 2000)
        summary_input_chars = self._message_budget() - len(SUMMARY_SYSTEM_PROMPT) - task_chars - 512
        try:
            summarized = await summarize_history(
                self.model,
                messages,
                keep_recent_chars=target // 2,
                max_input_chars=summary_input_chars,
                max_summary_tokens=min(self.max_summary_tokens, getattr(getattr(self.model, "config", None), "max_tokens", self.max_summary_tokens)),
            ) if summary_input_chars > 0 else None
        except Exception as exc:
            logger.warning("Context summarization failed: %s", exc)
            summarized = None
        if summarized is not None:
            compacted, response = summarized
            self._summary_calls += 1
            self._metrics.model_calls += 1
            self._metrics.prompt_tokens += response.prompt_tokens
            self._metrics.completion_tokens += response.completion_tokens
            self._metrics.latency_ms += response.latency_ms
            if sum(message_size(message) for message in compacted) <= target:
                self._emit_event("context_summarized", f"Summarized earlier history ({before} -> {sum(message_size(m) for m in compacted)} chars)", task_id=task.task_id)
                return compacted
            messages = compacted

        self._emit_event("context_compacted", "Archived and compacted earlier history to fit the context budget", task_id=task.task_id)
        return compress_messages(messages, target, archive=lambda _: reference)

    def _fit_tool_output(self, tool_call, output: str) -> str:
        """Cap one tool result to the per-result budget, saving the full
        text where read_file can page through it when possible."""
        if tool_call.name == ToolName.READ_FILE.value:
            fitted = self._fit_read_file(tool_call, output)
            if fitted != output and self._project_root() is None:
                fitted = self._retain_truncated_output(tool_call, output, fitted)
            return fitted
        spill_path = spill_display = None
        root = self._project_root()
        if root is not None:
            safe_id = "".join(ch for ch in str(tool_call.id) if ch.isalnum() or ch in "-_") or "call"
            spill_display = f"{self.tool_output_dir}/{tool_call.name}-{safe_id}.txt"
            try:
                spill_path = state_path(root, spill_display)
            except (OSError, ValueError) as exc:
                logger.warning("Not saving truncated tool output: %s", exc)
                spill_display = None
        fitted = truncate_tool_output(
            output,
            max_chars=self.max_tool_output_chars,
            max_lines=self.max_tool_output_lines,
            spill_path=spill_path,
            spill_display=spill_display,
        )
        if fitted != output and spill_path is None:
            fitted = self._retain_truncated_output(tool_call, output, fitted)
        return fitted

    def _retain_truncated_output(self, tool_call, output: str, fitted: str) -> str:
        reference = self._archive_context([Message(
            role="tool", content=output, tool_call_id=tool_call.id, name=tool_call.name,
        )])
        return fitted + f"\n[Full untrusted tool output archived: {reference}]"

    def _fit_read_file(self, tool_call, output: str) -> str:
        """read_file is already paged, so spilling its output would only
        produce another file to page through, and a head/tail cut would
        leave its "continue at start_line=N" hint skipping the dropped
        middle. Keep a head of whole lines and point at the next line."""
        lines = output.splitlines(keepends=True)
        if len(output) <= self.max_tool_output_chars and len(lines) <= self.max_tool_output_lines:
            return output
        try:
            first = max(1, int(tool_call.arguments.get("start_line") or 1))
        except (TypeError, ValueError):
            first = 1
        budget = self.max_tool_output_chars - 200  # room for the notice
        page: list[str] = []
        used = 0
        for line in lines:
            if page and (len(page) >= self.max_tool_output_lines or used + len(line) > budget):
                break
            page.append(line[:budget])
            used += len(page[-1])
        last = first + len(page) - 1
        return "".join(page).rstrip("\n") + (
            f"\n[Showing lines {first}-{last}; the rest was cut to fit the context budget. "
            f"Call read_file with start_line={last + 1} to continue.]"
        )

    def _project_root(self) -> Path | None:
        """The workspace read_file is rooted at, or None if this agent can't
        use read_file -- a saved output it can't read back is useless."""
        get_tool = getattr(self.tool_registry, "get_tool", None)
        has_permission = getattr(self.tool_registry, "has_permission", None)
        if get_tool is None or has_permission is None:
            return None
        try:
            if not has_permission(self.role, ToolName.READ_FILE):
                return None
            root = getattr(get_tool(ToolName.READ_FILE), "project_root", None)
        except Exception:
            return None
        return Path(root) if isinstance(root, str) else None

    def _build_messages(self, task: AgentTask) -> list[Message]:
        """Build the initial message list for a task."""
        messages = [Message(role="system", content=self.system_prompt)]
        
        # Build user message from task
        user_content = self._format_task(task)
        messages.append(Message(role="user", content=user_content))
        
        return messages
    
    def _format_task(self, task: AgentTask) -> str:
        """Format a task into a user message. Override in subclasses for custom formatting."""
        parts = [
            "## Objective",
            task.objective,
            "",
            "## Working context",
        ]

        if task.context.architecture:
            parts.append(f"## Repository Map\n{task.context.architecture}")
        if task.context.git_history:
            parts.append("### Git context\n" + "\n".join(f"- {entry}" for entry in task.context.git_history))
        if task.context.relevant_symbols:
            parts.append("### Relevant symbols\n" + "\n".join(f"- {symbol}" for symbol in task.context.relevant_symbols))
        if task.context.guidelines:
            parts.append("## Project Guidelines (AGENTS.md)\n" + task.context.guidelines)

        if task.files:
            parts.append("## Relevant Files\n" + "\n".join(f"- {f}" for f in task.files))

        if task.constraints:
            parts.append("## Constraints\n" + "\n".join(f"- {c}" for c in task.constraints))

        if task.success_criteria:
            parts.append("## Success Criteria\n" + "\n".join(f"- {s}" for s in task.success_criteria))

        if task.context.file_contents:
            parts.append("## File Contents")
            for path, content in task.context.file_contents.items():
                parts.append(f"### {path}\n```\n{content}\n```")

        if task.context.error_context:
            parts.append(f"## Error Context\n```\n{task.context.error_context}\n```")

        if task.context.previous_findings:
            parts.append("## Previous Findings\n" + "\n".join(f"- {f}" for f in task.context.previous_findings))

        return "\n\n".join(parts)
    
    def _build_response(
        self,
        task: AgentTask,
        model_response: ModelResponse,
        status: TaskStatus,
        issues: list[str] | None = None,
    ) -> AgentResponse:
        """Build an AgentResponse from the model's final response."""
        return AgentResponse(
            task_id=task.task_id,
            status=status,
            summary=model_response.content,
            files_changed=sorted(self._files_changed),
            tests_run=self._tests_run,
            tests_passed=self._tests_passed,
            issues=issues or [],
            metrics=self._metrics,
        )
    
    def _emit_event(self, event_type: str, message: str, **kwargs):
        """Emit an observability event."""
        if self._event_callback:
            event = AgentEvent(
                source=self.role.value.upper(),
                event_type=event_type,
                message=message,
                **kwargs,
            )
            self._event_callback(event)

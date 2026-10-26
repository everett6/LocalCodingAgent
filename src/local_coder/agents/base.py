"""Base agent with tool-calling loop."""
from __future__ import annotations
import asyncio
import json
import logging
import hashlib
import os
import re
from pathlib import Path
from datetime import datetime
from typing import Callable

from local_coder.types import (
    AgentRole, AgentTask, AgentResponse, AgentState, AgentPhase, TaskStatus,
    Message, ToolResult, ModelResponse, AgentMetrics, AgentEvent, TestResult,
)
from local_coder.context.compression import compress_messages, message_size
from local_coder.core.runtime import AgentRun

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        model,  # LocalModel protocol
        tool_registry,  # ToolRegistry
        event_callback: Callable[[AgentEvent], None] | None = None,
        context_window_chars: int = 24000,
        compact_context_chars: int = 12000,
        drafter=None,  # Optional[SpeculativeDrafter]
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
        
        message_budget = self.context_window_chars
        context_tokens = getattr(getattr(self.model, "config", None), "context_length", None)
        if context_tokens:
            catalog_chars = len(json.dumps(self.tool_registry.get_schemas_for_role(self.role)))
            output_tokens = getattr(self.model.config, "max_tokens", 4096)
            message_budget = min(message_budget, context_tokens * 3 - catalog_chars - output_tokens * 3)
        for iteration in range(self.state.iteration, self.max_iterations):
            if sum(message_size(message) for message in messages) > message_budget:
                before = sum(message_size(message) for message in messages)
                messages = compress_messages(messages, max(256, min(self.compact_context_chars, message_budget * 2 // 3)), self._archive_context)
                self._emit_event("context_compacted", "Archived and compacted agent context", task_id=task.task_id,
                                 data={"before_chars": before, "after_chars": sum(message_size(m) for m in messages)})
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
                        tools=self.tool_registry.get_schemas_for_role(self.role),
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
                    self._tests_run.append(tc.arguments.get("test_path") or "project tests")
                    self._test_outcomes[tc.arguments.get("test_path") or "project tests"] = result.success
                    self._tests_passed = all(self._test_outcomes.values())
                    if not result.success:
                        self.state.phase = AgentPhase.REFLECTING
                    self.state.test_results.append(TestResult(
                        test_name=tc.arguments.get("test_path") or "project tests",
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
                    content=tool_output or "Tool completed without output.",
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
            parts.append(f"### Repository architecture\n{task.context.architecture}")
        if task.context.git_history:
            parts.append("### Git context\n" + "\n".join(f"- {entry}" for entry in task.context.git_history))
        if task.context.relevant_symbols:
            parts.append("### Relevant symbols\n" + "\n".join(f"- {symbol}" for symbol in task.context.relevant_symbols))

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

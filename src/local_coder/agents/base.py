"""Base agent with tool-calling loop."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from local_coder.types import (
    AgentRole, AgentTask, AgentResponse, AgentState, AgentPhase, TaskStatus,
    Message, ToolResult, ModelResponse, AgentMetrics, AgentEvent, TestResult, ToolName,
)
from local_coder.context.compression import (
    compress_messages,
    message_chars,
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
        self.tool_registry = tool_registry
        self._event_callback = event_callback
        self._metrics = AgentMetrics()
        # Keep track of files changed during this agent's execution
        self._files_changed: set[str] = set()
        self._tests_run: list[str] = []
        self._tests_passed = True
        self.state: AgentState | None = None
        self.context_window_chars = context_window_chars
        self.compact_context_chars = compact_context_chars
        self._last_failing_call_signature: str | None = None
        self._repeated_failure_count: int = 0
        self._stagnation_warned: bool = False
        self.drafter = drafter
        self.max_tool_output_chars = max_tool_output_chars or max(2000, min(16000, context_window_chars // 8))
        self._last_prompt_tokens = 0

    async def execute(self, task: AgentTask) -> AgentResponse:
        """Execute a task through the tool-calling loop, optionally primed
        with a speculative draft of the first relevant file's edit."""
        draft_prediction = None
        if self.drafter is not None and task.files:
            draft_prediction = await self._maybe_draft(task)

        response = await self._execute_loop(task, draft_prediction)

        if draft_prediction is not None:
            self._settle_draft(draft_prediction, response)
        return response

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

    async def _execute_loop(self, task: AgentTask, draft_prediction) -> AgentResponse:
        # Build initial messages
        messages = self._build_messages(task)
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
        self._files_changed.clear()
        self._metrics = AgentMetrics()
        self._tests_run = []
        self._tests_passed = True
        self._last_failing_call_signature = None
        self._repeated_failure_count = 0
        self._stagnation_warned = False
        self._last_prompt_tokens = 0
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
        
        for iteration in range(self.max_iterations):
            if self._needs_compaction(messages):
                messages = await self._compact(messages, task)
                self.state.messages = list(messages)
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
                        messages,
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
                self.state.phase = AgentPhase.DONE if self._tests_passed else AgentPhase.FAILED
                self.state.finished_at = datetime.now()
                return self._build_response(
                    task,
                    response,
                    TaskStatus.COMPLETED if self._tests_passed else TaskStatus.FAILED,
                    issues=[] if self._tests_passed else ["Verification tests failed"],
                )
            
            # Add assistant message with tool calls
            messages.append(Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            ))
            self.state.messages = list(messages)
            self.state.tool_calls.extend(response.tool_calls)
            
            # Execute tool calls
            for tc in response.tool_calls:
                if self.state.tool_calls_used >= self.max_tool_calls:
                    self.state.phase = AgentPhase.FAILED
                    self.state.errors.append("Exceeded maximum tool-call limit")
                    self.state.finished_at = datetime.now()
                    return self._build_response(
                        task,
                        ModelResponse(content="Tool-call limit reached."),
                        TaskStatus.FAILED,
                        issues=["Exceeded maximum tool-call limit"],
                    )
                if tc.name == "run_tests" and self.state.test_runs >= self.max_test_runs:
                    self.state.phase = AgentPhase.FAILED
                    self.state.errors.append("Exceeded maximum test-run limit")
                    self.state.finished_at = datetime.now()
                    return self._build_response(
                        task,
                        ModelResponse(content="Test-run limit reached."),
                        TaskStatus.FAILED,
                        issues=["Exceeded maximum test-run limit"],
                    )
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

                self._metrics.tool_calls += 1
                self.state.tool_calls_used += 1
                if tc.name in {"read_file", "search_files", "grep"}:
                    path = tc.arguments.get("path")
                    if path:
                        self.state.files_read.add(path)
                if result.files_changed:
                    self._metrics.files_written += len(result.files_changed)
                    self._files_changed.update(result.files_changed)
                    self.state.files_changed.update(result.files_changed)
                if tc.name == "run_tests":
                    self.state.phase = AgentPhase.VERIFYING
                    self.state.test_runs += 1
                    self._tests_run.append(tc.arguments.get("target") or tc.arguments.get("test_path") or "project tests")
                    self._tests_passed = self._tests_passed and result.success
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
    def _token_limit(self) -> int | None:
        config = getattr(self.model, "config", None)
        context_length = getattr(config, "context_length", None)
        max_tokens = getattr(config, "max_tokens", None)
        if not isinstance(context_length, int) or context_length <= 0:
            return None
        reserve = max_tokens if isinstance(max_tokens, int) else 0
        return max(int(context_length * self.context_token_threshold) - reserve, context_length // 4)

    def _needs_compaction(self, messages: list[Message]) -> bool:
        if message_chars(messages) > self.context_window_chars:
            return True
        limit = self._token_limit()
        return limit is not None and self._last_prompt_tokens > limit

    async def _compact(self, messages: list[Message], task: AgentTask) -> list[Message]:
        """Bring the conversation back under budget, cheapest step first:
        clear old tool output, then have the model summarize the middle of
        the history, then (if that fails) drop it with compress_messages."""
        before = message_chars(messages)
        target = self.compact_context_chars
        limit = self._token_limit()
        if limit is not None and self._last_prompt_tokens > limit:
            # The server says we're out of tokens even though the character
            # count may look fine, so aim below the current size instead.
            target = min(target, int(before * limit / self._last_prompt_tokens * 0.7))
        # Compaction changes the prompt, so the last reported size no longer
        # applies; the next generate() call reports the new one.
        self._last_prompt_tokens = 0

        messages, freed = prune_tool_outputs(messages, protect_chars=target // 2)
        if freed:
            self._emit_event(
                "context_pruned",
                f"Cleared {freed} chars of old tool output",
                task_id=task.task_id,
            )
        if message_chars(messages) <= target:
            return messages

        try:
            summarized = await summarize_history(
                self.model,
                messages,
                keep_recent_chars=target // 2,
                max_input_chars=max(self.context_window_chars - target // 2, 4000),
                max_summary_tokens=self.max_summary_tokens,
            )
        except Exception as exc:
            logger.warning("Context summarization failed: %s", exc)
            summarized = None
        if summarized is not None:
            compacted, response = summarized
            self._metrics.model_calls += 1
            self._metrics.prompt_tokens += response.prompt_tokens
            self._metrics.completion_tokens += response.completion_tokens
            self._metrics.latency_ms += response.latency_ms
            if message_chars(compacted) <= target:
                self._emit_event(
                    "context_summarized",
                    f"Summarized earlier history ({before} -> {message_chars(compacted)} chars)",
                    task_id=task.task_id,
                )
                return compacted
            messages = compacted

        self._emit_event("context_compacted", "Dropped earlier history to fit the context budget", task_id=task.task_id)
        return compress_messages(messages, target)

    def _fit_tool_output(self, tool_call, output: str) -> str:
        """Cap one tool result to the per-result budget, saving the full
        text where read_file can page through it when possible."""
        if tool_call.name == ToolName.READ_FILE.value:
            return self._fit_read_file(tool_call, output)
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
        return truncate_tool_output(
            output,
            max_chars=self.max_tool_output_chars,
            max_lines=self.max_tool_output_lines,
            spill_path=spill_path,
            spill_display=spill_display,
        )

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
        parts = [f"## Objective\n{task.objective}"]

        if task.context.guidelines:
            parts.append("## Project Guidelines (AGENTS.md)\n" + task.context.guidelines)

        if task.files:
            parts.append("## Relevant Files\n" + "\n".join(f"- {f}" for f in task.files))
        
        if task.constraints:
            parts.append("## Constraints\n" + "\n".join(f"- {c}" for c in task.constraints))
        
        if task.success_criteria:
            parts.append("## Success Criteria\n" + "\n".join(f"- {s}" for s in task.success_criteria))
        
        if task.context.architecture:
            parts.append(f"## Repository Map\n{task.context.architecture}")

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

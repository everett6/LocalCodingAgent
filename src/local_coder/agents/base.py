"""Base agent with tool-calling loop."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
from typing import Callable

from local_coder.types import (
    AgentRole, AgentTask, AgentResponse, AgentState, AgentPhase, TaskStatus,
    Message, ToolResult, ModelResponse, AgentMetrics, AgentEvent, TestResult,
)
from local_coder.context.compression import compress_messages

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
            if sum(len(message.content) for message in messages) > self.context_window_chars:
                messages = compress_messages(messages, self.compact_context_chars)
                self.state.messages = list(messages)
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
                        messages,
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
                    self._tests_run.append(tc.arguments.get("test_path") or "project tests")
                    self._tests_passed = self._tests_passed and result.success
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

"""Base agent with tool-calling loop."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable

from local_coder.types import (
    AgentRole, AgentTask, AgentResponse, AgentState, AgentPhase, TaskStatus, TaskContext,
    Message, ToolCall, ToolResult, ModelResponse, AgentMetrics, AgentEvent, TestResult,
)

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base agent with model interaction and tool-calling loop."""
    
    role: AgentRole
    system_prompt: str
    max_iterations: int = 15  # Max tool-calling rounds
    
    def __init__(
        self,
        model,  # LocalModel protocol
        tool_registry,  # ToolRegistry
        event_callback: Callable[[AgentEvent], None] | None = None,
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
    
    async def execute(self, task: AgentTask) -> AgentResponse:
        """Execute a task through the tool-calling loop."""
        # Build initial messages
        messages = self._build_messages(task)
        self._files_changed.clear()
        self._metrics = AgentMetrics()
        self._tests_run = []
        self._tests_passed = True
        self.state = AgentState(
            task_id=task.task_id,
            objective=task.objective,
            max_iterations=self.max_iterations,
            started_at=datetime.now(),
        )
        self.state.phase = AgentPhase.EXECUTING
        self.state.messages = list(messages)
        
        for iteration in range(self.max_iterations):
            self.state.iteration = iteration + 1
            self._emit_event(
                "iteration_started",
                f"Starting tool loop iteration {iteration + 1}/{self.max_iterations}",
                task_id=task.task_id,
            )

            try:
                response = await self.model.generate(
                    messages,
                    temperature=self.model.config.temperature if hasattr(self.model, "config") else 0.2,
                    max_tokens=self.model.config.max_tokens if hasattr(self.model, "config") else 4096,
                    tools=self.tool_registry.get_schemas_for_role(self.role),
                )
            except Exception as exc:
                self.state.phase = AgentPhase.FAILED
                self.state.errors.append(f"Model generation failed: {exc}")
                self.state.finished_at = datetime.now()
                self._emit_event("model_error", str(exc), task_id=task.task_id)
                return self._build_response(
                    task,
                    ModelResponse(content="Model generation failed."),
                    TaskStatus.FAILED,
                    issues=[f"Model generation failed: {exc}"],
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
                if tc.name in {"read_file", "search_files", "grep"}:
                    path = tc.arguments.get("path")
                    if path:
                        self.state.files_read.add(path)
                if result.files_changed:
                    self._metrics.files_written += len(result.files_changed)
                    self._files_changed.update(result.files_changed)
                    self.state.files_changed.update(result.files_changed)
                if tc.name == "run_tests":
                    self._tests_run.append(tc.arguments.get("test_path") or "project tests")
                    self._tests_passed = self._tests_passed and result.success
                    self.state.test_results.append(TestResult(
                        test_name=tc.arguments.get("test_path") or "project tests",
                        passed=result.success,
                        duration_ms=result.duration_ms or 0.0,
                        error_message=None if result.success else result.output,
                        stdout=result.output,
                    ))
                if not result.success:
                    self.state.errors.append(result.error or result.output)
                
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

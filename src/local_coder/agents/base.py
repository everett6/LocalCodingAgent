"""Base agent with tool-calling loop."""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Callable

from local_coder.types import (
    AgentRole, AgentTask, AgentResponse, TaskStatus, TaskContext,
    Message, ToolCall, ToolResult, ModelResponse, AgentMetrics, AgentEvent,
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
    
    async def execute(self, task: AgentTask) -> AgentResponse:
        """Execute a task through the tool-calling loop."""
        # Build initial messages
        messages = self._build_messages(task)
        self._files_changed.clear()
        
        for iteration in range(self.max_iterations):
            # Get model response
            response = await self.model.generate(
                messages,
                temperature=self.model.config.temperature if hasattr(self.model, 'config') else 0.2,
                max_tokens=self.model.config.max_tokens if hasattr(self.model, 'config') else 4096,
                tools=self.tool_registry.get_schemas_for_role(self.role),
            )
            
            # Track metrics
            self._metrics.model_calls += 1
            self._metrics.prompt_tokens += response.prompt_tokens
            self._metrics.completion_tokens += response.completion_tokens
            self._metrics.latency_ms += response.latency_ms
            
            # If no tool calls, we're done
            if not response.tool_calls:
                return self._build_response(task, response, TaskStatus.COMPLETED)
            
            # Add assistant message with tool calls
            messages.append(Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            ))
            
            # Execute tool calls
            for tc in response.tool_calls:
                self._emit_event(
                    "tool_call",
                    f"Calling {tc.name}({json.dumps(tc.arguments)[:100]})",
                    task_id=task.task_id,
                )
                
                result = await self.tool_registry.execute_tool(
                    self.role, tc.name, tc.arguments
                )
                
                self._metrics.tool_calls += 1
                if result.files_changed:
                    self._metrics.files_written += len(result.files_changed)
                    self._files_changed.update(result.files_changed)
                
                # Add tool result as message
                messages.append(Message(
                    role="tool",
                    content=result.output if result.success else f"Error: {result.error}",
                    tool_call_id=tc.id,
                    name=tc.name,
                ))
        
        # Max iterations reached
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

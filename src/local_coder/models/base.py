"""Abstract base for local model backends."""
from __future__ import annotations
import abc
import time
from typing import AsyncIterator, Protocol, runtime_checkable

from local_coder.types import Message, ModelResponse, ModelConfig, ToolCall


@runtime_checkable
class LocalModel(Protocol):
    """Protocol for local model backends."""
    
    @property
    def config(self) -> ModelConfig: ...
    
    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stop: list[str] | None = None,
    ) -> ModelResponse: ...
    
    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...
    
    async def is_available(self) -> bool: ...
    
    async def get_model_info(self) -> dict: ...


class BaseModelBackend(abc.ABC):
    """Base class for model backends with shared functionality."""
    
    def __init__(self, config: ModelConfig):
        self._config = config
    
    @property
    def config(self) -> ModelConfig:
        return self._config
    
    @abc.abstractmethod
    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stop: list[str] | None = None,
    ) -> ModelResponse:
        ...
    
    @abc.abstractmethod
    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        ...
    
    @abc.abstractmethod
    async def is_available(self) -> bool:
        ...
    
    async def get_model_info(self) -> dict:
        return {
            "name": self._config.name,
            "backend": self._config.backend.value,
            "model_id": self._config.model_id,
            "context_length": self._config.context_length,
        }
    
    def _build_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal message format to API format."""
        result = []
        for msg in messages:
            d = {"role": msg.role, "content": msg.content}
            if msg.name:
                d["name"] = msg.name
            if msg.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": str(tc.arguments),
                        }
                    }
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                d["tool_call_id"] = msg.tool_call_id
            result.append(d)
        return result

"""Ollama backend implementation."""
from __future__ import annotations
import json
import time
from typing import AsyncIterator

import httpx

from local_coder.types import Message, ModelResponse, ModelConfig, ToolCall
from local_coder.models.base import BaseModelBackend


class OllamaBackend(BaseModelBackend):
    """Backend for Ollama running locally."""

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self.client = httpx.AsyncClient(base_url=config.base_url, timeout=300.0)

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stop: list[str] | None = None,
    ) -> ModelResponse:
        """Generate a response using Ollama API."""
        start_time = time.time()
        api_messages = self._build_messages(messages)
        
        options = {
            "temperature": temperature if temperature is not None else self.config.temperature,
            "num_predict": max_tokens if max_tokens is not None else self.config.max_tokens,
            "num_ctx": self.config.context_length,
        }
        if stop:
            options["stop"] = stop

        payload = {
            "model": self.config.model_id,
            "messages": api_messages,
            "options": options,
            "stream": False,
        }
        
        if tools:
            payload["tools"] = tools

        try:
            response = await self.client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            raise RuntimeError(f"Ollama API request failed: {e}") from e

        latency_ms = (time.time() - start_time) * 1000.0
        
        msg_data = data.get("message", {})
        content = msg_data.get("content", "")
        
        parsed_tool_calls = []
        if "tool_calls" in msg_data:
            for tc in msg_data["tool_calls"]:
                func = tc.get("function", {})
                parsed_tool_calls.append(ToolCall(
                    name=func.get("name", ""),
                    arguments=func.get("arguments", {})
                ))

        return ModelResponse(
            content=content,
            tool_calls=parsed_tool_calls,
            finish_reason=data.get("done_reason", "stop"),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
            latency_ms=latency_ms,
            model=self.config.model_id,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream response using Ollama API."""
        api_messages = self._build_messages(messages)
        options = {
            "temperature": temperature if temperature is not None else self.config.temperature,
            "num_predict": max_tokens if max_tokens is not None else self.config.max_tokens,
            "num_ctx": self.config.context_length,
        }

        payload = {
            "model": self.config.model_id,
            "messages": api_messages,
            "options": options,
            "stream": True,
        }

        try:
            async with self.client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if "message" in data and "content" in data["message"]:
                            yield data["message"]["content"]
                    except json.JSONDecodeError:
                        continue
        except httpx.HTTPError as e:
            raise RuntimeError(f"Ollama stream failed: {e}") from e

    async def is_available(self) -> bool:
        """Check if Ollama server is running and model is pulled."""
        try:
            response = await self.client.get("/api/tags", timeout=5.0)
            if response.status_code != 200:
                return False
                
            data = response.json()
            models = [m.get("name") for m in data.get("models", [])]
            # Handle exact match or base match (e.g., 'llama3' matching 'llama3:latest')
            return any(m == self.config.model_id or m.startswith(f"{self.config.model_id}:") for m in models)
        except httpx.HTTPError:
            return False

    async def get_model_info(self) -> dict:
        """Get model details from Ollama."""
        try:
            response = await self.client.post(
                "/api/show",
                json={"name": self.config.model_id},
                timeout=10.0
            )
            response.raise_for_status()
            data = response.json()
            
            info = await super().get_model_info()
            info.update({
                "format": data.get("details", {}).get("format"),
                "family": data.get("details", {}).get("family"),
                "parameter_size": data.get("details", {}).get("parameter_size"),
                "quantization_level": data.get("details", {}).get("quantization_level"),
            })
            return info
        except httpx.HTTPError:
            return await super().get_model_info()

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

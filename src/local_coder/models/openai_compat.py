"""OpenAI-compatible backend implementation."""
from __future__ import annotations
import json
import time
from typing import AsyncIterator

import httpx

from local_coder.types import Message, ModelResponse, ModelConfig, ToolCall
from local_coder.models.base import BaseModelBackend


class OpenAICompatibleBackend(BaseModelBackend):
    """Backend for OpenAI-compatible APIs (vLLM, LMStudio, llama.cpp server)."""

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self.client = httpx.AsyncClient(base_url=config.base_url, timeout=300.0)

    def _convert_tools(self, tools: list[dict] | None) -> list[dict] | None:
        """Convert standard tools dict to OpenAI format if needed."""
        if not tools:
            return None
        # Assuming tools are already in OpenAI JSON schema format
        return tools

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stop: list[str] | None = None,
    ) -> ModelResponse:
        """Generate a response using an OpenAI-compatible API."""
        start_time = time.time()
        api_messages = self._build_messages(messages)
        
        payload = {
            "model": self.config.model_id,
            "messages": api_messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
            "stream": False,
        }
        
        if stop:
            payload["stop"] = stop
            
        openai_tools = self._convert_tools(tools)
        if openai_tools:
            payload["tools"] = openai_tools
            payload["tool_choice"] = "auto"

        try:
            response = await self.client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            raise RuntimeError(f"OpenAI API request failed: {e}") from e

        latency_ms = (time.time() - start_time) * 1000.0
        
        choice = data.get("choices", [{}])[0]
        msg_data = choice.get("message", {})
        content = msg_data.get("content") or ""
        
        parsed_tool_calls = []
        if "tool_calls" in msg_data and msg_data["tool_calls"]:
            for tc in msg_data["tool_calls"]:
                func = tc.get("function", {})
                args_str = func.get("arguments", "{}")
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}
                    
                parsed_tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=args
                ))

        usage = data.get("usage", {})

        return ModelResponse(
            content=content,
            tool_calls=parsed_tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            latency_ms=latency_ms,
            model=data.get("model", self.config.model_id),
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream response using an OpenAI-compatible API."""
        api_messages = self._build_messages(messages)
        
        payload = {
            "model": self.config.model_id,
            "messages": api_messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
            "stream": True,
        }

        try:
            async with self.client.stream("POST", "/v1/chat/completions", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                        
                    try:
                        data = json.loads(data_str)
                        choices = data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            if "content" in delta and delta["content"]:
                                yield delta["content"]
                    except json.JSONDecodeError:
                        continue
        except httpx.HTTPError as e:
            raise RuntimeError(f"OpenAI stream failed: {e}") from e

    async def is_available(self) -> bool:
        """Check if the server is reachable and model exists."""
        try:
            response = await self.client.get("/v1/models", timeout=5.0)
            if response.status_code == 200:
                return True
            return False
        except httpx.HTTPError:
            return False

    async def get_model_info(self) -> dict:
        """Get model details from /v1/models."""
        try:
            response = await self.client.get(f"/v1/models/{self.config.model_id}", timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                info = await super().get_model_info()
                info.update({
                    "owner": data.get("owned_by"),
                    "created": data.get("created"),
                })
                return info
        except httpx.HTTPError:
            pass
            
        return await super().get_model_info()

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

"""Anthropic Messages API adapter using the existing httpx dependency."""
from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx

from local_coder.models.base import BaseModelBackend
from local_coder.types import Message, ModelConfig, ModelResponse, ToolCall


class AnthropicBackend(BaseModelBackend):
    def __init__(self, config: ModelConfig):
        super().__init__(config)
        key = config.resolve_api_key()
        if not key:
            raise ValueError("Set ANTHROPIC_API_KEY or supply a key in provider settings")
        self.headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        base_url = config.base_url
        if base_url == "http://localhost:11434":
            base_url = "https://api.anthropic.com/v1"
        self.client = httpx.AsyncClient(base_url=base_url, headers=self.headers, timeout=300.0)

    def _payload(self, messages, temperature=None, max_tokens=None, tools=None, stop=None):
        system = []
        converted = []
        for message in messages:
            if message.role == "system":
                system.append(message.content)
                continue
            if message.role == "tool":
                role = "user"
                blocks = [{"type": "tool_result", "tool_use_id": message.tool_call_id,
                           "content": message.content}]
            else:
                role = message.role
                blocks = ([{"type": "text", "text": message.content}] if message.content else [])
                blocks.extend({"type": "tool_use", "id": call.id, "name": call.name,
                               "input": call.arguments} for call in message.tool_calls or [])
            if not blocks:
                continue
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})
        payload = {"model": self.config.model_id, "messages": converted,
                   "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
                   "temperature": temperature if temperature is not None else self.config.temperature}
        if system:
            payload["system"] = "\n\n".join(system)
        if tools:
            payload["tools"] = [{"name": tool["function"]["name"],
                                 "description": tool["function"].get("description", ""),
                                 "input_schema": tool["function"]["parameters"]} for tool in tools]
        if stop:
            payload["stop_sequences"] = stop
        return payload

    async def generate(self, messages: list[Message], *, temperature=None, max_tokens=None,
                       tools=None, stop=None) -> ModelResponse:
        start = time.monotonic()
        try:
            response = await self.client.post("/messages", json=self._payload(messages, temperature, max_tokens, tools, stop))
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Anthropic request failed (HTTP {exc.response.status_code}); check key, model and quota") from None
        except httpx.HTTPError:
            raise RuntimeError("Anthropic connection failed; check the endpoint and network") from None
        blocks = data.get("content", [])
        usage = data.get("usage", {})
        prompt = usage.get("input_tokens", 0)
        output = usage.get("output_tokens", 0)
        return ModelResponse(
            content="\n".join(block["text"] for block in blocks if block.get("type") == "text"),
            tool_calls=[ToolCall(id=b["id"], name=b["name"], arguments=b["input"])
                        for b in blocks if b.get("type") == "tool_use"],
            finish_reason=data.get("stop_reason") or "stop", prompt_tokens=prompt,
            completion_tokens=output, total_tokens=prompt + output,
            latency_ms=(time.monotonic() - start) * 1000, model=data.get("model", self.config.model_id))

    async def stream(self, messages: list[Message], *, temperature=None, max_tokens=None) -> AsyncIterator[str]:
        payload = self._payload(messages, temperature, max_tokens)
        payload["stream"] = True
        try:
            async with self.client.stream("POST", "/messages", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = json.loads(line[6:])
                    if data.get("type") == "error":
                        raise RuntimeError("Anthropic stream failed; retry the request")
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta["text"]
        except httpx.HTTPError:
            raise RuntimeError("Anthropic stream connection failed") from None

    async def is_available(self) -> bool:
        try:
            response = await self.client.get("/models", timeout=5)
            return response.is_success
        except httpx.HTTPError:
            return False

    async def close(self):
        await self.client.aclose()

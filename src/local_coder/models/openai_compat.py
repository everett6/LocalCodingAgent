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
            # Relative to base_url, which is expected to already include the
            # server's own "/v1" prefix (the OpenAI SDK convention, e.g.
            # "http://localhost:8090/v1") -- httpx joins a leading-slash path
            # onto base_url's own path rather than replacing it, so a
            # "/v1/..." path here would request ".../v1/v1/...".
            response = await self.client.post("/chat/completions", json=payload)
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
            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
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
            response = await self.client.get("/models", timeout=5.0)
            if response.status_code == 200:
                return True
            return False
        except httpx.HTTPError:
            return False

    async def get_model_info(self) -> dict:
        """Get model details from /v1/models."""
        try:
            response = await self.client.get(f"/models/{self.config.model_id}", timeout=5.0)
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

    def _server_root_url(self) -> str:
        """The /slots endpoint lives at the llama-server root, not under the
        configured base_url's "/v1" path (base_url is expected to already
        include "/v1" -- see generate()'s comment)."""
        base = str(self.client.base_url).rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base

    async def save_slot(self, filename: str, slot_id: int = 0) -> bool:
        """Persist a slot's KV cache to disk so it survives a server
        restart, via llama-server's /slots API (requires the server to be
        started with --slot-save-path; a 400 here most likely means it
        wasn't). Best-effort: returns False rather than raising on any
        failure -- this is purely a latency optimization for the next
        prefill, and its absence must never break a run.
        """
        try:
            response = await self.client.post(
                f"{self._server_root_url()}/slots/{slot_id}",
                params={"action": "save"},
                json={"filename": filename},
                timeout=120.0,
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def restore_slot(self, filename: str, slot_id: int = 0) -> bool:
        """Reload a previously saved slot KV cache from disk. A subsequent
        generate() call whose messages share a prefix with what was saved
        will skip re-computing that shared prefix (llama-server's own
        longest-common-prefix prompt-cache matching, enabled by default).
        Best-effort, see save_slot()."""
        try:
            response = await self.client.post(
                f"{self._server_root_url()}/slots/{slot_id}",
                params={"action": "restore"},
                json={"filename": filename},
                timeout=120.0,
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False

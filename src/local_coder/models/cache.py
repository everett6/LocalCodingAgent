"""Bounded application-level prompt/result cache for local model helpers."""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

from local_coder.types import Message, ModelConfig


def prompt_cache_key(config: ModelConfig, messages: list[Message]) -> str:
    """Build a model-aware key without including mutable object identity."""
    payload = {
        "model": config.model_id,
        "backend": config.backend.value,
        "messages": [message.model_dump(mode="json") for message in messages],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class PromptCache:
    """Small TTL/LRU cache for safe, repeatable helper prompts.

    This caches application results, not model KV state. Mutable coding-agent
    tool calls should bypass it; it is intended for speculative predictions.
    """

    def __init__(self, max_entries: int = 128, ttl_seconds: float = 300.0):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        created, value = entry
        if time.monotonic() - created >= self.ttl_seconds:
            del self._entries[key]
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        self._entries[key] = (time.monotonic(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
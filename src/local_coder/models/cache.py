"""Bounded application-level prompt/result cache for local model helpers."""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Any

from local_coder.types import Message, ModelConfig


def prompt_cache_key(config: ModelConfig, messages: list[Message]) -> str:
    """Build a model-aware key without including mutable object identity.

    Hashes incrementally, message by message, instead of first building one
    large JSON string for the whole prompt and then hashing that. With a
    large context window (this project's config points at a 65536-token
    local server -- see README.md) a single cached prompt can be hundreds of
    thousands of characters, and the old approach paid for two full copies
    of it (the joined JSON string, then its UTF-8 encoding) on every
    drafter call just to throw both away once hashed. update() consumes
    each message's bytes as they're produced instead.
    """
    hasher = hashlib.sha256()
    hasher.update(config.model_id.encode())
    hasher.update(b"\x00")
    hasher.update(config.backend.value.encode())
    for message in messages:
        hasher.update(b"\x00")
        hasher.update(message.model_dump_json().encode())
    return hasher.hexdigest()


class PromptCache:
    """Small TTL/LRU cache for safe, repeatable helper prompts.

    This caches application results, not model KV state. Mutable coding-agent
    tool calls should bypass it; it is intended for speculative predictions.

    Bounded on two axes, evicting oldest-first on either: entry count
    (max_entries) and total tracked size (max_total_chars, a str(value)
    length proxy). Entry count alone doesn't actually bound memory -- one
    cached value from a role configured with a large max_tokens could be
    much heavier than the other 127, and the entry-count limit would never
    notice. That matters more now that this project's config drives a
    65536-token local server (see README.md) instead of a small one.
    """

    def __init__(self, max_entries: int = 128, ttl_seconds: float = 300.0, max_total_chars: int = 2_000_000):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_total_chars = max_total_chars
        self._entries: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()
        self._total_chars = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        created, value, _size = entry
        if time.monotonic() - created >= self.ttl_seconds:
            self._evict(key)
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        size = len(str(value))
        if key in self._entries:
            self._total_chars -= self._entries[key][2]
        self._entries[key] = (time.monotonic(), value, size)
        self._entries.move_to_end(key)
        self._total_chars += size
        while self._entries and (len(self._entries) > self.max_entries or self._total_chars > self.max_total_chars):
            self._evict(next(iter(self._entries)))

    def _evict(self, key: str) -> None:
        _created, _value, size = self._entries.pop(key)
        self._total_chars -= size

    def clear(self) -> None:
        self._entries.clear()
        self._total_chars = 0
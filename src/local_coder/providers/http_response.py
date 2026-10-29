"""Bounded identity-encoded provider bodies and incremental SSE framing.

Callers own the HTTPX stream context so early exits and cancellation close it.
Compressed responses are rejected before raw iteration; no decompressor runs.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
import json

import httpx

MAX_COMPLETION_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_ERROR_BYTES = 128_000
MAX_EVENT_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
IDENTITY_HEADERS = {"Accept-Encoding": "identity"}
_PARSER_CHUNK_BYTES = 64 * 1024


async def bounded_raw(response: httpx.Response, limit: int) -> AsyncIterator[bytes]:
    """Count transport bytes before retaining them, independent of declared size.

    A transport can deliver one chunk that crosses the limit; it is rejected
    before accumulation. Prebuffered custom transports cannot be unbuffered here.
    """
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in {"", "identity"}:
        raise RuntimeError("Provider returned unsupported content encoding; identity is required")
    declared = response.headers.get("content-length")
    if declared is not None:
        if not declared.isascii() or not declared.isdecimal() or len(declared) > 20:
            raise RuntimeError("Provider returned an invalid response length")
        if int(declared) > limit:
            raise RuntimeError("Provider response exceeded the byte limit")
    if response.is_stream_consumed:
        body = response.content
        if len(body) > limit:
            raise RuntimeError("Provider response exceeded the byte limit")
        yield body
        return
    size = 0
    async for chunk in response.aiter_raw():
        size += len(chunk)
        if size > limit:
            raise RuntimeError("Provider response exceeded the byte limit")
        yield chunk


async def bounded_body(response: httpx.Response, limit: int) -> bytes:
    body = bytearray()
    async for chunk in bounded_raw(response, limit):
        body.extend(chunk)
    return bytes(body)


def json_object(body: bytes | str) -> dict:
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8-sig")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("Expected object")
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeError("Provider returned an invalid JSON response or stream event") from None
    return data


async def bounded_json(response: httpx.Response, limit: int | None = None) -> dict:
    return json_object(await bounded_body(response, MAX_COMPLETION_BYTES if limit is None else limit))


class _SSEDecoder:
    """Data-field subset of SSE: comments, BOM, CR/LF and multiline dispatch."""

    def __init__(self, limit: int):
        self.limit = limit
        self.line = bytearray()
        self.data: list[str] = []
        self.event_bytes = 0
        self.pending_cr = False
        self.first_line = True

    def feed(self, chunk: bytes):
        if not chunk:
            return
        if self.pending_cr and chunk.startswith(b"\n"):
            chunk = chunk[1:]
        self.pending_cr = chunk.endswith(b"\r")
        chunk = chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = len(chunk) if end < 0 else end
            self.event_bytes += stop - start + (end >= 0)
            if self.event_bytes > self.limit:
                raise RuntimeError("Provider stream event exceeded the byte limit")
            self.line.extend(chunk[start:stop])
            if end < 0:
                break
            try:
                line = self.line.decode("utf-8-sig" if self.first_line else "utf-8")
            except UnicodeError:
                raise RuntimeError("Provider returned an invalid UTF-8 stream event") from None
            self.first_line = False
            self.line.clear()
            start = end + 1
            if not line:
                event = "\n".join(self.data) if self.data else None
                self.data.clear()
                self.event_bytes = 0
                if event is not None:
                    yield event
            elif not line.startswith(":"):
                field, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    self.data.append(value)


async def sse_data(response: httpx.Response, *, event_limit: int | None = None,
                   stream_limit: int | None = None) -> AsyncIterator[str]:
    decoder = _SSEDecoder(MAX_EVENT_BYTES if event_limit is None else event_limit)
    async for chunk in bounded_raw(response, MAX_STREAM_BYTES if stream_limit is None else stream_limit):
        for offset in range(0, len(chunk), _PARSER_CHUNK_BYTES):
            for event in decoder.feed(chunk[offset:offset + _PARSER_CHUNK_BYTES]):
                yield event
    # SSE discards unterminated events at EOF. Provider callers separately
    # require a recognized terminal event; EOF cannot complete partial calls.

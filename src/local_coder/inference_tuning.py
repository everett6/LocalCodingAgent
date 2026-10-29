"""Operator-triggered, bounded measurements of the configured local runtime only."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import struct
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from local_coder.memory.context_store import _clean
from local_coder.statedir import state_path

_PROMPT = "List eight small prime numbers, separated by spaces. Do not explain."
_VARIANTS = {"context", "gpu", "cache", "draft"}
_HISTORY = ".local-coder/inference/tuning.json"


class TuningCancelled(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe(value):
    # The schema is app-owned. Preserve numeric token telemetry while redacting
    # secret patterns in string values; generic metadata key redaction would
    # replace legitimate tokens_per_second measurements with a string.
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _file_identity(value):
    """Bounded fingerprint includes file stat and head/tail samples, not full weights."""
    path = Path(value).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Configured model/runtime must be a regular local file")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        digest.update(stream.read(1048576))
        if before.st_size > 1048576:
            stream.seek(max(1048576, before.st_size - 1048576))
            digest.update(stream.read(1048576))
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("Configured model/runtime changed during fingerprinting")
    return {"path": str(path), "size": after.st_size, "mtime_ns": after.st_mtime_ns,
            "inode": after.st_ino, "sample_sha256": digest.hexdigest(), "method": "stat_and_head_tail_1MiB"}


def fingerprint(manager, settings):
    runtime = manager._runtime(settings["runtime_path"])
    return {"settings": copy.deepcopy(settings), "runtime": _file_identity(runtime),
            "model": _file_identity(settings["model_path"]),
            "draft": _file_identity(settings["draft_model_path"]) if settings["draft_model_path"] else None}


def _tokenizer_signature(value):
    """Compare complete bounded tokenizer metadata; unknown/partial means unavailable."""
    scalars = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    entries = {}
    with Path(value).open("rb") as stream:
        def read(count):
            if count < 0 or stream.tell() + count > 16 * 1048576:
                raise ValueError("Draft compatibility metadata exceeds inspection limit")
            raw = stream.read(count)
            if len(raw) != count:
                raise ValueError("Draft compatibility metadata is incomplete")
            return raw

        def string():
            count = struct.unpack("<Q", read(8))[0]
            return read(count)

        def consume(kind):
            if kind in scalars:
                read(scalars[kind])
            elif kind == 8:
                string()
            elif kind == 9:
                subtype, count = struct.unpack("<IQ", read(12))
                if subtype == 9 or count > 1000000:
                    raise ValueError("Draft compatibility metadata exceeds inspection limit")
                if subtype in scalars:
                    read(scalars[subtype] * count)
                elif subtype == 8:
                    for _ in range(count):
                        string()
                else:
                    raise ValueError("Unknown tokenizer metadata type")
            else:
                raise ValueError("Unknown tokenizer metadata type")

        magic, version, tensors, count = struct.unpack("<4sIQQ", read(24))
        if magic != b"GGUF" or version not in (2, 3) or not tensors or count > 4096:
            raise ValueError("Draft compatibility requires complete GGUF metadata")
        for _ in range(count):
            key = string().decode("utf-8")
            kind = struct.unpack("<I", read(4))[0]
            start = stream.tell()
            consume(kind)
            end = stream.tell()
            if key == "general.architecture" or (key.startswith("tokenizer.") and key != "tokenizer.chat_template"):
                stream.seek(start)
                entries[key] = hashlib.sha256(struct.pack("<I", kind) + read(end - start)).hexdigest()
        if not {"general.architecture", "tokenizer.ggml.model", "tokenizer.ggml.tokens"} <= entries.keys():
            raise ValueError("Draft tokenizer compatibility is unknown")
    return entries


def draft_compatibility(settings, capabilities):
    if not settings.get("draft_model_path"):
        return False, "No configured draft model"
    if not ({"--spec-draft-model", "--model-draft"} & set(capabilities.get("flags", []))):
        return False, "Installed runtime lacks a draft model flag"
    try:
        if _tokenizer_signature(settings["model_path"]) != _tokenizer_signature(settings["draft_model_path"]):
            return False, "Configured draft tokenizer/architecture differs from target"
    except (OSError, ValueError, UnicodeError, struct.error):
        return False, "Configured draft compatibility cannot be established within metadata bounds"
    return True, "Matching bounded GGUF tokenizer and architecture metadata; runtime trial still required"


def measure_stream(endpoint, settings, cancel, timeout=20):
    """Use only the manager's ephemeral authenticated loopback endpoint."""
    parsed = urlsplit(endpoint.get("base_url", ""))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.path != "/v1" or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.port:
        raise ValueError("Benchmark requires the app-owned loopback /v1 endpoint")
    key = endpoint.get("api_key")
    if not isinstance(key, str) or not key:
        raise ValueError("Benchmark requires ephemeral runtime authentication")
    started = time.monotonic()
    first = None
    completion_tokens = None
    total_bytes = 0
    finished = False
    with httpx.Client(timeout=httpx.Timeout(timeout, read=5), trust_env=False, follow_redirects=False) as client:
        with client.stream("POST", endpoint["base_url"] + "/chat/completions",
                           headers={"Authorization": "Bearer " + key},
                           json={"model": endpoint["model"], "messages": [{"role": "user", "content": _PROMPT}],
                                 "temperature": 0, "seed": 42, "max_tokens": 32, "stream": True,
                                 "stream_options": {"include_usage": True}, "cache_prompt": settings["prompt_cache"]}) as response:
            response.raise_for_status()
            def lines():
                nonlocal total_bytes
                pending = b""
                for chunk in response.iter_bytes():
                    if cancel.is_set():
                        raise TuningCancelled()
                    total_bytes += len(chunk)
                    if time.monotonic() - started > timeout or total_bytes > 1048576:
                        raise ValueError("Benchmark response exceeded time/size bounds")
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        yield line.decode("utf-8").rstrip("\r")
                if pending:
                    yield pending.decode("utf-8")
            for line in lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    finished = True
                    break
                item = json.loads(raw)
                count = item.get("usage", {}).get("completion_tokens") if isinstance(item.get("usage"), dict) else None
                if type(count) is int and 0 <= count <= 64:
                    completion_tokens = count
                choices = item.get("choices", [])
                if choices and isinstance(choices[0], dict) and any(choices[0].get("delta", {}).get(field) for field in ("content", "reasoning_content")):
                    if first is None:
                        first = time.monotonic()
    if cancel.is_set():
        raise TuningCancelled()
    if not finished or first is None:
        raise ValueError("Runtime did not complete the bounded streaming benchmark")
    # Chunks are not tokens. Throughput is unknown without actual runtime usage.
    elapsed = time.monotonic() - started
    throughput = completion_tokens / elapsed if completion_tokens and elapsed > 0 else None
    return {"ttft_ms": (first - started) * 1000, "tokens_per_second": throughput,
            "completion_tokens": completion_tokens, "elapsed_seconds": time.monotonic() - started,
            "token_count_source": "runtime_usage" if completion_tokens is not None else "unknown",
            "throughput_scope": "end_to_end_request_including_prefill",
            "memory_bytes": None, "vram_bytes": None, "memory_telemetry": "unavailable"}


class InferenceTuner:
    def __init__(self, root, manager, *, transport=None, shutdown=None, lifecycle_gate=None):
        self.root, self.manager = Path(root), manager
        self._measure = transport or measure_stream
        self._shutdown = shutdown or (lambda: False)
        self._lifecycle_gate = lifecycle_gate if lifecycle_gate is not None else threading.RLock()
        self._lock = threading.RLock()
        self._trusted = {}

    @staticmethod
    def options(value):
        if not isinstance(value, dict) or set(value) - {"variants", "repeats"}:
            raise ValueError("Supply only variants and repeats; metrics cannot be imported")
        variants, repeats = value.get("variants", ["context", "gpu", "cache", "draft"]), value.get("repeats", 2)
        if not isinstance(variants, list) or len(variants) > 4 or any(not isinstance(item, str) for item in variants) or len(set(variants)) != len(variants) or any(item not in _VARIANTS for item in variants):
            raise ValueError("Choose up to four unique context, gpu, cache or draft variants")
        if type(repeats) is not int or not 2 <= repeats <= 3:
            raise ValueError("Benchmark repeats must be 2 or 3")
        return variants, repeats

    def _wait_ready(self, cancel, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            if cancel.is_set() or self._shutdown():
                raise TuningCancelled()
            status = self.manager.status()
            if status.get("ready"):
                return status
            if status.get("state") not in {"starting", "running"} or time.monotonic() >= deadline:
                raise ValueError("Local runtime failed to become ready within the benchmark startup bound")
            cancel.wait(.05)

    def _start_if_allowed(self, cancel=None):
        # The host changes its shutdown flag and performs its final stop under
        # this same gate. Never hold it while waiting for readiness or joining.
        with self._lifecycle_gate:
            if self._shutdown() or (cancel is not None and cancel.is_set()):
                return False
            self.manager.start()
            return True

    def _restore(self, settings, running):
        self.manager.stop()
        self.manager.configure(settings)
        if running and self._start_if_allowed():
            try:
                self._wait_ready(threading.Event())
            except TuningCancelled:
                if not self._shutdown():
                    raise

    def _samples(self, settings, repeats, cancel):
        samples = []
        if cancel.is_set() or self._shutdown():
            raise TuningCancelled()
        # One warmup keeps first-load effects separate from measured repeats.
        self._measure(self.manager.endpoint_config(), settings, cancel)
        for _ in range(repeats):
            if cancel.is_set():
                raise TuningCancelled()
            sample = self._measure(self.manager.endpoint_config(), settings, cancel)
            for key in ("ttft_ms", "tokens_per_second"):
                value = sample.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                    raise ValueError("Runtime returned invalid benchmark telemetry")
            samples.append(sample)
        return {"samples": samples, "ttft_ms": statistics.median(row["ttft_ms"] for row in samples),
                "tokens_per_second": statistics.median(row["tokens_per_second"] for row in samples) if all(row["tokens_per_second"] is not None for row in samples) else None,
                "memory_bytes": None, "vram_bytes": None}

    def run(self, options, cancel, progress=None):
        variants, repeats = self.options(options)
        original = self.manager.status()
        if original["state"] not in {"stopped", "running", "error"}:
            raise ValueError("Wait for runtime startup, shutdown or installation before benchmarking")
        settings, running = copy.deepcopy(original["settings"]), original.get("running", False)
        identity = fingerprint(self.manager, settings)
        record = {"id": uuid4().hex, "created_at": _now(), "status": "running", "fingerprint": identity,
                  "baseline_settings": settings, "was_running": running, "repeats": repeats,
                  "prompt_sha256": hashlib.sha256(_PROMPT.encode()).hexdigest(), "max_output_tokens": 32,
                  "results": [], "restored": False, "error": None}
        try:
            if cancel.is_set():
                raise TuningCancelled()
            self.manager.stop()
            self.manager.configure(settings)
            if not self._start_if_allowed(cancel):
                raise TuningCancelled()
            capabilities = self._wait_ready(cancel).get("capabilities", {})
            baseline = {"variant": "baseline", "settings": settings, "status": "measured",
                        **self._samples(settings, repeats, cancel), "improved": False}
            record["results"].append(baseline)
            if progress:
                progress(copy.deepcopy(record))
            for variant in variants:
                if cancel.is_set():
                    raise TuningCancelled()
                candidate = copy.deepcopy(settings)
                available, reason = True, ""
                flags = set(capabilities.get("flags", []))
                if variant == "context":
                    candidate["context_size"] = max(512, settings["context_size"] // 2)
                    available = candidate["context_size"] != settings["context_size"]
                    reason = "Context is already at the minimum"
                elif variant == "gpu":
                    available = "--gpu-layers" in flags
                    candidate["gpu_layers"] = 0 if settings["gpu_layers"] else 16
                    reason = "Installed runtime lacks GPU layer controls"
                elif variant == "cache":
                    available = bool({"--cache-ram", "--cache-reuse"} & flags)
                    candidate["prompt_cache"] = not settings["prompt_cache"]
                    reason = "Installed runtime lacks prompt cache controls"
                elif variant == "draft":
                    available, reason = draft_compatibility(settings, capabilities)
                    if available and not capabilities.get("speculative_decoding"):
                        available, reason = False, "Configured draft was not activated by the installed runtime"
                    if available:
                        candidate["draft_model_path"] = ""
                        reason = "Compare configured compatible draft against target-only decoding"
                row = {"variant": variant, "settings": candidate, "improved": False}
                if not available:
                    row.update(status="unavailable", reason=reason)
                else:
                    try:
                        self.manager.stop()
                        self.manager.configure(candidate)
                        if not self._start_if_allowed(cancel):
                            raise TuningCancelled()
                        ready = self._wait_ready(cancel)
                        if settings["draft_model_path"] and variant != "draft" and not ready.get("capabilities", {}).get("speculative_decoding"):
                            raise ValueError("Configured draft mode was not activated by this runtime")
                        row.update(status="measured", **self._samples(candidate, repeats, cancel))
                        speed, base_speed = row["tokens_per_second"], baseline["tokens_per_second"]
                        row["improved"] = bool(speed and base_speed and speed >= base_speed * 1.05 and row["ttft_ms"] <= baseline["ttft_ms"] * 1.10)
                        row["reason"] = "At least 5% higher measured output throughput, TTFT within 10%" if row["improved"] else "No measured improvement under the output-throughput/TTFT thresholds"
                    except TuningCancelled:
                        raise
                    except Exception:
                        row.update(status="failed", reason="Candidate startup or measurement failed; no improvement established")
                record["results"].append(row)
                if progress:
                    progress(copy.deepcopy(record))
            if fingerprint(self.manager, settings) != identity:
                raise ValueError("Model/runtime files changed during measurement")
            record["status"] = "completed"
        except TuningCancelled:
            record["status"] = "cancelled"
        except Exception:
            record.update(status="failed", error="Benchmark startup or measurement failed; review local runtime configuration")
        finally:
            try:
                self._restore(settings, running)
                record["restored"] = True
                record["stopped_for_engine_shutdown"] = bool(self._shutdown())
            except Exception:
                record.update(status="failed", error="Baseline restoration failed; stop inference and restore the saved baseline settings")
            record["finished_at"] = _now()
            self._save(record)
            if record["status"] == "completed" and record["restored"]:
                with self._lock:
                    self._trusted[record["id"]] = copy.deepcopy(record)
                    while len(self._trusted) > 20:
                        del self._trusted[next(iter(self._trusted))]
        return self._public(record)

    def _history_path(self):
        path = state_path(self.root, _HISTORY)
        if path.exists() and (path.stat().st_nlink > 1 or path.stat().st_size > 2000000):
            raise ValueError("Unsafe or oversized tuning history")
        return path

    def _read(self):
        path = self._history_path()
        if not path.exists():
            return []
        records = json.loads(path.read_text())
        if not isinstance(records, list) or len(records) > 20:
            raise ValueError("Invalid tuning history")
        return records

    def _save(self, record):
        with self._lock:
            records = self._read()
            records = [row for row in records if row.get("id") != record["id"]]
            records.append(_safe(record))
            target = self._history_path()
            descriptor, temporary = tempfile.mkstemp(prefix="tuning-", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(records[-20:], output)
                self._history_path()
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def _public(self, record):
        result = copy.deepcopy(record)
        with self._lock:
            trusted = result.get("id") in self._trusted
        result["can_apply"] = trusted and result.get("status") == "completed" and self.manager.status().get("settings") == result.get("baseline_settings") and any(row.get("improved") for row in result.get("results", []))
        result["measurement_trust"] = "measured_this_engine_session" if trusted else "historical_display_only"
        return _safe(result)

    def history(self):
        with self._lock:
            return [self._public(row) for row in reversed(self._read())]

    def apply(self, record_id, variant):
        with self._lock:
            record = copy.deepcopy(self._trusted.get(record_id))
        if not record:
            raise ValueError("Re-run the benchmark in this engine session before applying")
        row = next((row for row in record["results"] if row["variant"] == variant and row.get("improved") and row["status"] == "measured"), None)
        if row is None:
            raise ValueError("Only a measured improved candidate can be applied")
        original = self.manager.status()
        if original.get("state") not in {"running", "stopped", "error"}:
            raise ValueError("Wait for local runtime lifecycle operations before applying")
        settings = original["settings"]
        if settings != record["baseline_settings"] or fingerprint(self.manager, settings) != record["fingerprint"]:
            raise ValueError("Model, runtime or settings changed; benchmark again before applying")
        try:
            self._restore(row["settings"], original.get("running", False))
        except Exception:
            self._restore(settings, original.get("running", False))
            raise ValueError("Candidate activation failed; baseline restored") from None
        return {"applied": True, "record_id": record_id, "variant": variant, "settings": _safe(row["settings"])}

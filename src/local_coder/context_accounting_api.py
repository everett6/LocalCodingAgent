"""Authenticated host mixin for explicit offline tokenizer configuration."""
from __future__ import annotations

from local_coder.context.token_count import (
    LocalTokenCounter, adapter_availability, tokenizer_config, load_counter_for_project,
)

TOKENIZER_PATH = ".local-coder/context/tokenizer.json"


class ContextAccountingAPI:
    def _init_context_accounting_api(self):
        self._tokenizer_error = None
        try:
            saved = self._read_harness_state(TOKENIZER_PATH)
            self._tokenizer_settings = tokenizer_config(self._harness_root, saved or {"enabled": False})
        except Exception:
            self._tokenizer_settings = {"enabled": True}
            self._tokenizer_error = "Saved local tokenizer unavailable; conservative estimates retained"
        self._context_token_counter = None

    def context_token_counter(self):
        """Call when constructing a job ledger; never performs network I/O."""
        settings = self._tokenizer_settings
        if not settings.get("enabled") or self._tokenizer_error:
            return None
        if self._context_token_counter is None:
            self._context_token_counter = LocalTokenCounter(
                self._harness_root / settings["path"], settings["model_id"], settings["template_sha256"],
            )
        return self._context_token_counter

    def _context_accounting_get(self, path, query):
        if path == "/context/debugger":
            from local_coder.context.telemetry import ContextTelemetry
            from local_coder.memory.context_store import ContextMemory
            if not isinstance(query, dict) or set(query) - {"task_id", "limit"}:
                raise ValueError("Unsupported context debugger query")
            def single(key, default):
                value = query.get(key, default)
                return value[0] if isinstance(value, list) and len(value) == 1 else value
            limit = int(single("limit", 20))
            result = ContextTelemetry(self._harness_root).inspect(task_id=single("task_id", None), limit=limit)
            result["retrievals"] = ContextMemory(self._harness_root).retrievals(limit=limit)
            return result
        if path != "/context/accounting":
            return None
        if not isinstance(query, dict) or query:
            raise ValueError("Context accounting does not accept query parameters")
        ledger = getattr(self, "current_budget", None)
        counter = self._context_token_counter
        return {
            "availability": adapter_availability(),
            "tokenizer": counter.status() if counter else {
                "configured": bool(self._tokenizer_settings.get("enabled")),
                "available": False if self._tokenizer_error else None, "semantics": "estimated",
                "reason": self._tokenizer_error or ("Not loaded" if self._tokenizer_settings.get("enabled") else "Not configured"),
                "provider_serialization_verified": False,
            },
            "ledger": ledger.accounting_status() if ledger else None,
            "admission_method": "conservative_utf8_with_output_reserve",
        }

    def _context_accounting_post(self, path, body):
        if path != "/context/tokenizer":
            return None
        with self._harness_idle(), self._harness_state_lock:
            settings = tokenizer_config(self._harness_root, body)
            counter = None
            if settings.get("enabled"):
                counter = LocalTokenCounter(
                    self._harness_root / settings["path"], settings["model_id"], settings["template_sha256"],
                )
            self._write_harness_state(TOKENIZER_PATH, settings)
            self._tokenizer_settings = settings
            self._tokenizer_error = None
            self._context_token_counter = counter
            return self._context_accounting_get("/context/accounting", {})

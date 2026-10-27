"""Private routing preferences and conservative cached-metadata synthesis.

Reading or saving preferences never downloads a model, probes a provider, or
starts inference. Runtime credentials remain on in-memory ModelConfig objects.
"""
from __future__ import annotations

import json
import math
import re

from local_coder.harness_api import _object
from local_coder.integrations.mcp import redact_data
from local_coder.providers.catalog import is_openrouter
from local_coder.models.manager import ModelManager, is_local_model
from local_coder.types import ModelBackend
from local_coder.statedir import state_path, write_state_file

SETTINGS_PATH = ".local-coder/routing/settings.json"
ECONOMY_ID = "deepseek/deepseek-v4.1-flash-1017"
_FIELDS = {"auto_enabled", "economy_model", "local_classifier", "classifier_timeout_seconds", "allow_cross_provider"}
_BOOLEAN_FIELDS = {"auto_enabled", "allow_cross_provider", "use_loaded_local_classifier"}


def _validated(values):
    _object(values, _FIELDS | {"use_loaded_local_classifier"})
    for key, value in values.items():
        if key in _BOOLEAN_FIELDS:
            if type(value) is not bool:
                raise ValueError(f"{key} must be a boolean")
        elif key == "classifier_timeout_seconds":
            if type(value) not in {int, float} or not math.isfinite(value) or not .25 <= value <= 5:
                raise ValueError("Classifier timeout must be from 0.25 to 5 seconds")
        elif value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
            or redact_data(value) != value
        ):
            raise ValueError(f"{key} must name a configured model key, or be null")
    return values


class AutoRoutingAPI:
    def _routing_preferences(self):
        path = state_path(self.project_root, SETTINGS_PATH)
        if not path.exists():
            return {}
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 8192:
            raise ValueError("Routing preferences must be a small ordinary private file")
        values = json.loads(path.read_text())
        _validated(values)
        return values

    def _routing_settings_post(self, body):
        _validated(body)
        with self._harness_idle():
            preferences = self._routing_preferences() | body
            write_state_file(self.project_root, SETTINGS_PATH, json.dumps(preferences, allow_nan=False))
            return self._routing_settings_status()

    def _routing_settings_status(self):
        from local_coder.orchestrator.config_loader import load_config

        config = load_config(self.config_path, project_root=self.project_root,
                             overrides=getattr(self, "config_overrides", None))
        provider = self._selected_provider_config()
        return self._apply_auto_routing(config, provider)

    def _apply_auto_routing(self, config, provider):
        """Apply user preferences to a fresh config using only cached metadata."""
        preferences = self._routing_preferences()
        config.routing = config.routing.model_copy(update={key: value for key, value in preferences.items() if key in _FIELDS}, deep=True)
        routing = config.routing
        selected = provider or config.models.get(getattr(self, "model_name", None) or "default") or next(iter(config.models.values()), None)
        reason = "Automatic routing is disabled."
        economy = None
        if routing.auto_enabled:
            if routing.economy_model:
                economy = config.models.get(routing.economy_model)
                reason = "Configured economy model is available." if economy else "Configured economy model key was not found."
            else:
                reason = "No eligible economy candidate: select an official paid OpenRouter model and cache its catalog."
                if (selected is not None and selected.backend == ModelBackend.OPENAI_COMPAT
                        and is_openrouter(selected)
                        and ModelManager._endpoint(selected) == ("https", "openrouter.ai", 443, "/api/v1")
                        and not selected.model_id.endswith(":free")):
                    primary = self.catalog_service.cached_model(selected.model_id)
                    candidate = self.catalog_service.cached_model(ECONOMY_ID)
                    rates = ("input_cost_per_million", "output_cost_per_million", "request_cost_usd")
                    def priced(row):
                        return bool(row and row.get("stale") is not True and row.get("pricing_complete") is True and all(
                            type(row.get(key)) in {int, float} and math.isfinite(row[key]) and row[key] >= 0 for key in rates))
                    if (priced(primary) and not primary.get("is_free") and priced(candidate)
                            and candidate.get("supports_tools") is True
                            and type(candidate.get("context_length")) is int and candidate["context_length"] > 0
                            and all(candidate[key] <= primary[key] for key in rates)
                            and any(candidate[key] < primary[key] for key in rates)):
                        context = min(selected.context_length, candidate["context_length"])
                        output = candidate.get("max_output_tokens")
                        maximum = min(selected.max_tokens, context)
                        if type(output) is int and output > 0:
                            maximum = min(maximum, output)
                        economy = selected.model_copy(update={
                            "name": "_routing_economy", "model_id": ECONOMY_ID,
                            "profile": "openrouter_deepseek", "context_length": context,
                            "max_tokens": maximum, "auto_context": False,
                            "context_source": "cached catalog (user ceiling)",
                            **{key: candidate[key] for key in rates},
                        }, deep=True)
                        config.models[economy.name] = economy
                        routing.economy_model = economy.name
                        reason = "Cached prices support an economy candidate; uncertain and security tasks keep the selected model."
        classifier = config.models.get(routing.local_classifier) if routing.local_classifier else None
        if classifier is not None and not is_local_model(classifier):
            classifier = None
        configured = bool(routing.local_classifier)
        classifier_ready = False
        if classifier is not None:
            loaded = self.local_inference_provider()
            classifier_ready = bool(loaded is not None
                and loaded.model_id == classifier.model_id
                and loaded.base_url.rstrip("/") == classifier.base_url.rstrip("/"))
        use_loaded = preferences.get("use_loaded_local_classifier", False)
        if routing.auto_enabled and not configured and use_loaded and selected is not None:
            if not is_local_model(selected):
                local = self.local_inference_provider()
                if local is not None and is_local_model(local):
                    classifier = local.model_copy(update={
                        "name": "_routing_classifier", "max_tokens": min(128, local.max_tokens),
                        "auto_context": False, "input_cost_per_million": 0,
                        "output_cost_per_million": 0, "request_cost_usd": 0,
                    }, deep=True)
                    config.models[classifier.name] = classifier
                    routing.local_classifier = classifier.name
                    classifier_ready = True
        def alias(value):
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) and redact_data(value) == value else None
        # Return only routing fields and bounded status, never provider dumps.
        settings = {key: getattr(config.routing, key) for key in _FIELDS}
        settings["economy_model"] = alias(settings["economy_model"]) if settings["economy_model"] != "_routing_economy" else None
        settings["local_classifier"] = alias(settings["local_classifier"]) if settings["local_classifier"] != "_routing_classifier" else None
        settings["use_loaded_local_classifier"] = use_loaded
        return {"settings": settings, "status": {
            "economy_available": economy is not None,
            "economy_model_id": ECONOMY_ID if economy is not None and economy.name == "_routing_economy" else None,
            "classifier_configured": configured, "classifier_ready": classifier_ready,
            "classifier_local_configured": classifier is not None,
            "classifier_state": "ready" if classifier_ready else "configured_unverified" if configured else "not_configured",
            "reason": reason,
        }}

"""Authenticated workbench routes for provider and on-device setup.

The host authenticates requests before invoking this mixin. Discovery reads
metadata only: selecting a file never starts a runtime or downloads weights.
"""

from __future__ import annotations

import re

from local_coder.harness_api import _object, _query
from local_coder.inference_discovery import DiscoveryManager
from local_coder.providers.catalog import CatalogService, is_openrouter
from local_coder.providers.profiles import ProviderProfileStore
from local_coder.providers.quota import FreeQuotaGuard
from local_coder.redteam_defaults import RedTeamDefaults
from local_coder.types import AgentEvent, ModelBackend


def _flag(query, name, default="0"):
    value = _query(query, name, default)
    if value not in {"0", "1"}:
        raise ValueError("Boolean query settings must be 0 or 1")
    return value == "1"


class ProviderAPI:
    def _init_provider_api(self):
        self.catalog_service = CatalogService(self.project_root)
        self.provider_profiles = ProviderProfileStore(self.project_root)
        self.free_quota_guard = FreeQuotaGuard(
            self.catalog_service, notice_callback=self._provider_quota_notice
        )
        self.runtime_discovery = DiscoveryManager()
        self.redteam_defaults = RedTeamDefaults(self.project_root)
        self._active_provider_profile = None

    def _provider_quota_notice(self, notice):
        self._event(
            AgentEvent(
                source="PROVIDER",
                event_type="provider_quota_exhausted",
                message=notice["message"],
                data=notice,
            )
        )

    def _selected_provider_config(self):
        if self._using_inference:
            return self.local_inference_provider()
        if self._provider is not None:
            return self._provider
        from local_coder.orchestrator.config_loader import load_config

        config = load_config(
            self.config_path,
            project_root=self.project_root,
            overrides=getattr(self, "config_overrides", None),
        )
        return config.models.get(self.model_name or "default") or next(
            iter(config.models.values()), None
        )

    def _provider_get(self, path, query):
        """Return (handled, payload); callers supply parsed query lists."""
        if path == "/providers/models":
            _object(query, {"source", "refresh"})
            if _query(query, "source", "openrouter") != "openrouter":
                raise ValueError("Choose OpenRouter or explicitly probe a local endpoint")
            return True, self.catalog_service.openrouter_models(
                refresh=_flag(query, "refresh")
            )
        if path == "/providers/usage":
            _object(query)
            config = self._selected_provider_config()
            if not is_openrouter(config):
                return True, {"provider": "other", "applies": False}
            return True, self.catalog_service.openrouter_usage(config)
        if path == "/providers/quota":
            _object(query, {"refresh"})
            config = self._selected_provider_config()
            status = self.free_quota_guard.status(
                config, refresh=_flag(query, "refresh")
            )
            blocked = bool(status.get("applies") and status.get("exhausted"))
            return True, {
                **status,
                "blocked": blocked,
                "model_id": config.model_id if config is not None else None,
                "reason": "OpenRouter daily free-request allowance exhausted" if blocked else None,
                "automatic_paid_fallback": False,
            }
        if path == "/providers/profiles":
            return True, {
                "profiles": self.provider_profiles.list(),
                "active_profile": self._active_provider_profile,
            }
        if path == "/inference/discovery":
            return True, self.runtime_discovery.status()
        if path == "/engagements/defaults":
            return True, self.redteam_defaults.status()
        return False, None

    def _provider_post(self, path, body):
        if path == "/providers/discover":
            _object(body, {"backend", "base_url", "api_key", "api_key_env"})
            backend = ModelBackend(body.get("backend", "openai_compatible"))
            if backend != ModelBackend.OPENAI_COMPAT:
                raise ValueError("Local metadata discovery requires an OpenAI-compatible server")
            config = dict(body)
            # An omitted key may reuse only the exact selected endpoint's key.
            # Edited endpoints never inherit credentials from another provider.
            previous = self._selected_provider_config()
            endpoint = body.get("base_url")
            if (
                not body.get("api_key")
                and not body.get("api_key_env")
                and previous is not None
                and previous.backend == backend
                and isinstance(endpoint, str)
                and endpoint.rstrip("/") == (previous.base_url or "").rstrip("/")
            ):
                config["api_key"] = previous.resolve_api_key()
            return True, self.catalog_service.probe_local(config)
        if path == "/providers/profiles/save":
            _object(body, {"name", "provider", "id"})
            with self._harness_idle():
                return True, self.provider_profiles.save(
                    body.get("name"), body.get("provider"), id=body.get("id")
                )
        match = re.fullmatch(r"/providers/profiles/([a-f0-9]{32})/(select|delete)", path)
        if match:
            _object(body, set())
            identity, action = match.groups()
            with self._harness_idle():
                if action == "delete":
                    deleted = self.provider_profiles.delete(identity)
                    if self._active_provider_profile == identity:
                        self._active_provider_profile = None
                    return True, {"deleted": deleted}
                record = self.provider_profiles.get(identity)
                result = self._update_settings(record["provider"])
                self._active_provider_profile = identity
                return True, {**result, "active_profile": identity}
        if path == "/inference/discover":
            _object(body, {"roots"})
            with self._harness_idle():
                return True, self.runtime_discovery.start(body.get("roots"))
        if path == "/inference/discovery/cancel":
            _object(body, set())
            return True, self.runtime_discovery.cancel()
        if path == "/inference/discovery/select":
            _object(body, {"runtime_path", "model_path"})
            with self._harness_idle():
                if self.inference.status()["state"] in {"starting", "running", "stopping", "installing"}:
                    raise ValueError("Stop inference before selecting another runtime or model")
                return True, self.runtime_discovery.select(**body)
        if path == "/engagements/defaults":
            with self._harness_idle():
                return True, self.redteam_defaults.update(body)
        return False, None

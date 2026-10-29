"""Doctor routes; the host authenticates callers before dispatch."""
from __future__ import annotations

from local_coder.doctor import diagnose, repair
from local_coder.harness_api import _object


class DoctorAPI:
    def _doctor_report(self):
        try:
            provider = self._selected_provider_config()
        except Exception:
            provider = None
        return diagnose(
            self.project_root, inference=getattr(self, "inference", None), provider=provider,
            discovery=getattr(self, "runtime_discovery", None),
        )

    def _doctor_get(self, path, query):
        if path != "/doctor":
            return False, None
        _object(query, set())
        return True, self._doctor_report()

    def _doctor_post(self, path, body):
        if path != "/doctor/repair":
            return False, None
        _object(body, {"action"})
        action = body.get("action")
        if not isinstance(action, str) or action not in {"reset_failed_runtime", "clear_stale_engine_descriptor", "ensure_private_project_state"}:
            raise ValueError("Choose a supported Doctor repair")
        with self._harness_idle():
            before = self._doctor_report()
            if action not in {item["action"] for item in before["repairs"]}:
                raise ValueError("Doctor repair is not available for the current state")
            try:
                verified = repair(self.project_root, action, getattr(self, "inference", None))
            except Exception:
                raise ValueError("Doctor repair could not be verified; refresh diagnostics") from None
            after = self._doctor_report()
            return True, {"action": action, "verified": verified, "before": before, "after": after}

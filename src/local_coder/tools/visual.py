"""Explicitly approved local browser verification, without browser installation."""
from __future__ import annotations

import json
import time
from typing import Any

from local_coder.approval import ApprovalCallback
from local_coder.tools.base import Tool
from local_coder.types import SafetyTier, ToolName, ToolResult
from local_coder.verification.visual import VisualVerifier, validate_visual_request


class VisualVerificationTool(Tool):
    name = ToolName.VERIFY_UI
    description = (
        "Inspect an already running local UI with explicit approval. Capture a private viewport PNG "
        "and check bounded selectors, visibility, text, page errors and horizontal overflow. "
        "Requires installed optional Playwright/Chromium. No server startup, remote browsing, "
        "page interaction, model calls or vision-quality claim."
    )
    # Navigation can execute application JavaScript; this is not a read-only tool.
    read_only = False
    mutating = True
    retry_safe = False
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "maxLength": 2048,
                    "description": "Already running literal 127.0.0.1 or [::1] HTTP(S) URL; no credentials/query/fragment"},
            "assertions": {"type": "array", "maxItems": 20, "items": {
                "type": "object", "properties": {
                    "selector": {"type": "string", "minLength": 1, "maxLength": 256},
                    "visible": {"type": "boolean"},
                    "text": {"type": "string", "minLength": 1, "maxLength": 512},
                }, "required": ["selector"], "additionalProperties": False}},
            "viewport": {"type": "object", "properties": {
                "width": {"type": "integer", "minimum": 320, "maximum": 1920},
                "height": {"type": "integer", "minimum": 240, "maximum": 1080},
            }, "required": ["width", "height"], "additionalProperties": False},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
        },
        "required": ["url"], "additionalProperties": False,
    }

    def __init__(self, project_root, approval_callback: ApprovalCallback | None = None, *, verifier=None, safety_harness=None):
        self.project_root = str(project_root)
        self.approval_callback = approval_callback
        self.verifier = verifier or VisualVerifier(project_root)
        self.safety_harness = safety_harness

    async def execute(self, url: str, assertions=None, viewport=None, timeout_seconds=15, **kwargs: Any) -> ToolResult:
        started = time.monotonic()

        def not_started(message):
            return ToolResult(success=False, output=message, data={"execution_outcome": "not_started"},
                              duration_ms=int((time.monotonic() - started) * 1000))

        try:
            if kwargs:
                raise ValueError("UI verification accepts no approval, output-path or execution overrides")
            request = validate_visual_request(url, assertions=assertions, viewport=viewport,
                                              timeout_seconds=timeout_seconds)
        except ValueError as exc:
            return not_started(str(exc))
        if getattr(self.safety_harness, "tier", None) == SafetyTier.TIER_1_PARANOIA:
            return not_started("Read-only safety tier does not permit browser execution.")
        if self.approval_callback is None:
            return not_started("Local UI navigation requires explicit host approval before starting Chromium.")
        try:
            approved = await self.approval_callback(
                f"Inspect local UI at {url} with {len(request['assertions'])} explicit assertions? "
                "This opens a fresh Chromium browser, runs the app's JavaScript, permits same-origin "
                "GET/HEAD/OPTIONS requests and saves a private screenshot. Other HTTP origins and "
                "WebSockets are blocked. Browser request filtering is not OS network isolation."
            )
        except Exception:
            return not_started("UI verification approval failed; no browser was started.")
        if approved is not True:
            return not_started("UI verification was not approved; no browser was started.")
        report = await self.verifier.verify(url, assertions=request["assertions"], viewport=request["viewport"],
                                            timeout_seconds=request["timeout_seconds"])
        unknown = bool(report.get("navigation_started") and report.get("artifact") is None)
        outcome = "unknown" if unknown else "completed" if report.get("navigation_started") else "not_started"
        return ToolResult(
            success=report["status"] in {"verified", "captured"},
            output=json.dumps(report, ensure_ascii=False),
            data={"visual_verification": report, "execution_outcome": outcome,
                  "mutation_completion_unknown": unknown},
            duration_ms=int((time.monotonic() - started) * 1000),
        )

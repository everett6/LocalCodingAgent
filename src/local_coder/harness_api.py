"""Trusted workbench routes for bounded context and runtime controls.

This mixin has no HTTP authentication layer: the existing workbench dispatcher
must authenticate the human before dispatching these methods. No model tool
exposes these configuration or approval methods.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote

from local_coder.context.language_service import ADAPTERS, LanguageService
from local_coder.core.approvals import ApprovalInbox
from local_coder.core.sandbox import SandboxRunner
from local_coder.core.snapshots import FileSnapshots
from local_coder.harness.evolution import EvolutionStore
from local_coder.integrations.mcp import MCPManager
from local_coder.memory.context_store import ContextMemory, _clean
from local_coder.models.profiles import validate_overlay
from local_coder.skills import SkillLibrary
from local_coder.statedir import state_path, write_state_file
from local_coder.types import HarnessSettings, SafetyTier

HARNESS_PATH = ".local-coder/harness/settings.json"
LANGUAGE_PATH = ".local-coder/language/settings.json"


def _object(value, allowed=None):
    if not isinstance(value, dict) or (
        allowed is not None and set(value) - set(allowed)
    ):
        raise ValueError("Expected an object containing only supported settings")
    return value


def _text(value, *, maximum=4000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        raise ValueError("Expected bounded text")
    return value


def _integer(value, low, high):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Integer setting is outside the supported range")
    return value


def _query(query, key, default):
    value = query.get(key, [default])
    if not isinstance(value, list) or len(value) != 1:
        raise ValueError("Query parameters must have one value")
    return value[0]


def _validate_settings(value):
    _object(value, HarnessSettings.model_fields)
    result = HarnessSettings.model_validate(value, strict=True)
    if result.max_cost_usd is not None and not math.isfinite(result.max_cost_usd):
        raise ValueError("Cost limit must be finite")
    validate_overlay(result.profile_overrides)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", result.sandbox_image):
        raise ValueError("Invalid sandbox image name")
    return result


def _language_config(value):
    _object(value, {"server_commands", "auto_discover"})
    commands = _object(value.get("server_commands", {}))
    if len(commands) > len(ADAPTERS) or set(commands) - {
        item.language for item in ADAPTERS
    }:
        raise ValueError("Unknown language adapter")
    auto_discover = value.get("auto_discover", False)
    if type(auto_discover) is not bool:
        raise ValueError("auto_discover must be a boolean")
    clean = {}
    for language, command in commands.items():
        if command is None:
            clean[language] = None
            continue
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 32
            or any(
                not isinstance(part, str)
                or not part
                or len(part) > 2048
                or "\x00" in part
                for part in command
            )
        ):
            raise ValueError("Language server commands must be bounded argument arrays")
        executable = Path(command[0])
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError(
                "Language server commands require an installed absolute executable path"
            )
        if executable.resolve().name.lower() in {
            "sh",
            "bash",
            "dash",
            "zsh",
            "fish",
            "env",
            "cmd",
            "cmd.exe",
            "powershell",
            "powershell.exe",
            "pwsh",
        }:
            raise ValueError(
                "Language servers cannot be shell or environment launcher commands"
            )
        if any(_clean(part) != part for part in command):
            raise ValueError("Language commands must not contain credential values")
        clean[language] = list(command)
    return {"server_commands": clean, "auto_discover": auto_discover}


class HarnessAPI:
    """Project-scoped services shared with the agent runtime by the host."""

    def _init_harness_api(self):
        root = getattr(self, "workspace", None) or self.project_root
        self._harness_root = Path(
            root if isinstance(root, (str, os.PathLike)) else root.root
        ).resolve()
        from local_coder.orchestrator.config_loader import load_config

        base = load_config(
            getattr(self, "config_path", None),
            project_root=str(self._harness_root),
            overrides=getattr(self, "config_overrides", None),
        )
        self._harness_base = _validate_settings(base.harness.model_dump(mode="json"))
        self._harness_base_tier = base.safety_tier
        self._harness_override = None
        self._harness_tier_override = None
        self._harness_state_lock = threading.RLock()
        self._harness_fallback_job_lock = threading.Lock()
        saved = self._read_harness_state(HARNESS_PATH)
        if saved is not None:
            _object(saved, {"settings", "safety_tier"})
            self._harness_override = _validate_settings(saved.get("settings", {}))
            if saved.get("safety_tier") is not None:
                self._harness_tier_override = SafetyTier(saved["safety_tier"])
        language = self._read_harness_state(LANGUAGE_PATH)
        self._language_settings = _language_config(language or {})
        self.approvals = ApprovalInbox()
        self.context_memory = ContextMemory(self._harness_root)
        self.file_snapshots = FileSnapshots(self._harness_root)
        self._skill_library = SkillLibrary(self._harness_root)
        self._sandbox_probe = None

    def _read_harness_state(self, relative):
        target = state_path(self._harness_root, relative)
        if not target.exists():
            return None
        if (
            not target.is_file()
            or target.stat().st_size > 64_000
            or target.stat().st_nlink != 1
        ):
            raise ValueError("Invalid private settings file")
        target.chmod(0o600)
        return json.loads(target.read_text(encoding="utf-8"))

    def _write_harness_state(self, relative, value):
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        if len(encoded.encode()) > 64_000:
            raise ValueError("Private configuration exceeds 64 KB")
        target = state_path(self._harness_root, relative)
        if target.exists() and target.stat().st_nlink != 1:
            raise ValueError("Private settings cannot be hard links")
        write_state_file(self._harness_root, relative, encoded + "\n")
        target.chmod(0o600)
        target.parent.chmod(0o700)

    @contextmanager
    def _harness_idle(self):
        lock = getattr(self, "_job_lock", self._harness_fallback_job_lock)
        if not lock.acquire(blocking=False):
            raise ValueError(
                "Stop the active task; this operation requires an idle engine"
            )
        try:
            if getattr(self, "active_job", None):
                raise ValueError(
                    "Stop the active task; this operation requires an idle engine"
                )
            yield
        finally:
            lock.release()

    def harness_settings(self):
        with self._harness_state_lock:
            return (self._harness_override or self._harness_base).model_copy(deep=True)

    def harness_safety_tier(self):
        with self._harness_state_lock:
            return self._harness_tier_override

    def language_settings(self):
        with self._harness_state_lock:
            return copy.deepcopy(self._language_settings)

    def _language_service(self):
        return LanguageService(self._harness_root, **self.language_settings())

    def _harness_status(self):
        settings = self.harness_settings()
        ledger = getattr(self, "current_budget", None)
        budget = ledger.status() if ledger is not None else None
        if budget is not None:
            budget = {key: value for key, value in budget.items() if key != "entries"}
        tier = self.harness_safety_tier() or (
            SafetyTier.TIER_5_YOLO
            if getattr(self, "yolo", False)
            else self._harness_base_tier
        )
        current = getattr(self, "current_registry", None)
        sandbox = self._sandbox_probe or getattr(current, "sandbox", None) or SandboxRunner(settings.sandbox_backend, settings.sandbox_image)
        return {
            "settings": settings.model_dump(mode="json"),
            "safety_tier": tier.value,
            "budget": budget,
            "sandbox": sandbox.status(),
            "evolution": _clean(EvolutionStore(self._harness_root).history()[-100:]),
            "language": self._language_service().status(),
        }

    def _harness_get(self, path, query):
        _object(query)
        if path in {"/harness", "/harness/settings"}:
            return self._harness_status()
        if path == "/memory":
            return {
                "memories": self.context_memory.list_memories(
                    _integer(_query(query, "limit", "50"), 0, 200)
                )
            }
        if path == "/memory/status":
            return self.context_memory.status()
        if path == "/memory/search":
            return {
                "items": self.context_memory.search(
                    _text(_query(query, "q", ""), empty=True),
                    limit=_integer(_query(query, "limit", "8"), 0, 50),
                    char_budget=_integer(_query(query, "budget", "4000"), 0, 12000),
                )
            }
        if path == "/integrations/mcp":
            return MCPManager(self._harness_root).status()
        if path == "/integrations/skills":
            return {"skills": self._skill_library.list()}
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if len(parts) == 3 and parts[:2] == ["integrations", "skills"]:
            return self._skill_library.read(parts[2])
        if path in {"/language", "/language/status"}:
            return {
                **self._language_service().status(),
                "config": self.language_settings(),
            }
        if path == "/language/context":
            return self._language_context(
                {
                    "path": _query(query, "path", ""),
                    "line": _integer(_query(query, "line", "1"), 1, 10000000),
                    "character": _integer(_query(query, "character", "0"), 0, 1000000),
                    "budget": _integer(_query(query, "budget", "4000"), 128, 16000),
                }
            )
        if path == "/snapshots":
            return {"snapshots": self.file_snapshots.list()}
        if len(parts) == 3 and parts[0] == "snapshots" and parts[2] == "diff":
            result = self.file_snapshots.diff(parts[1])
            for item in result["files"]:
                name = Path(item["path"]).name.lower()
                if (
                    name.startswith(".env")
                    or name
                    in {
                        "credentials",
                        "credentials.json",
                        "secrets.json",
                        "secrets.yaml",
                        "secrets.yml",
                        ".netrc",
                        ".npmrc",
                        ".pypirc",
                        "id_rsa",
                        "id_ed25519",
                    }
                    or Path(name).suffix
                    in {".pem", ".key", ".p12", ".pfx", ".keystore"}
                ):
                    raise ValueError("Credential file snapshots are not displayed")
            return _clean(result)
        if path == "/approvals":
            return {
                "approvals": [
                    item
                    for item in self.approvals.list()
                    if item["status"] == "pending"
                ]
            }
        return None

    def _language_context(self, body):
        _object(body, {"path", "line", "character", "budget", "query"})
        source = _text(body.get("path"), maximum=2048)
        line = _integer(body.get("line", 1), 1, 10000000)
        character = _integer(body.get("character", 0), 0, 1000000)
        budget = _integer(body.get("budget", 4000), 128, 16000)
        query = _text(body.get("query", ""), empty=True)

        async def run():
            service = self._language_service()
            try:
                return await service.context(
                    source,
                    line=line,
                    character=character,
                    query=query,
                    char_budget=budget,
                )
            finally:
                await service.close()

        return _clean(asyncio.run(run()))

    def _harness_post(self, path, body):
        _object(body)
        if path == "/harness/probe-sandbox":
            _object(body, set())
            with self._harness_idle():
                from local_coder.core.sandbox import SandboxUnavailable
                settings = self.harness_settings()
                runner = SandboxRunner(settings.sandbox_backend, settings.sandbox_image)
                self._sandbox_probe = runner
                try:
                    result = asyncio.run(runner.run("def run(value):\n    return {'ready': True}\n", {}, timeout=5))
                    if not result["success"]:
                        runner._status = {**runner.status(), "ready": False, "reason": "The isolated Python helper check failed"}
                except SandboxUnavailable as error:
                    runner._status = {**runner.status(), "ready": False, "reason": str(error)}
                return {"sandbox": runner.status()}
        if path in {"/harness/configure", "/harness/settings"}:
            _object(body, {"settings", "safety_tier"})
            with self._harness_idle(), self._harness_state_lock:
                raw = _object(body.get("settings", {}), HarnessSettings.model_fields)
                settings = _validate_settings(
                    {**self.harness_settings().model_dump(mode="json"), **raw}
                )
                tier = self._harness_tier_override
                if "safety_tier" in body:
                    tier = (
                        SafetyTier(body["safety_tier"])
                        if body["safety_tier"] is not None
                        else None
                    )
                self._write_harness_state(
                    HARNESS_PATH,
                    {
                        "settings": settings.model_dump(mode="json"),
                        "safety_tier": tier.value if tier else None,
                    },
                )
                self._harness_override, self._harness_tier_override = settings, tier
                self._sandbox_probe = None
                return self._harness_status()
        if path == "/integrations/mcp/configure":
            with self._harness_idle():
                return MCPManager(self._harness_root).configure(body)
        if path == "/integrations/mcp/probe":
            _object(body, {"server"})
            server = _text(body.get("server"), maximum=100)

            async def probe():
                manager = MCPManager(self._harness_root)
                try:
                    return {"tools": await manager.list_tools(server)}
                finally:
                    await manager.close()

            with self._harness_idle():
                return asyncio.run(probe())
        if path == "/integrations/skills/save":
            _object(body, {"skill", "title", "content", "description"})
            return self._skill_library.save(
                body.get("skill"),
                body.get("title"),
                body.get("content"),
                body.get("description", "Custom reference skill"),
            )
        if path == "/language/configure":
            with self._harness_idle(), self._harness_state_lock:
                config = _language_config(body)
                self._write_harness_state(LANGUAGE_PATH, config)
                self._language_settings = config
                return self._harness_get("/language", {})
        if path == "/language/context":
            return self._language_context(body)
        if path == "/memory/index":
            _object(body, {"paths"})
            paths = body.get("paths")
            if paths is not None and (
                not isinstance(paths, list)
                or len(paths) > 500
                or any(not isinstance(item, str) or len(item) > 2048 for item in paths)
            ):
                raise ValueError("Index paths must be bounded workspace paths")
            return self.context_memory.index_code(paths)
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if len(parts) == 3 and parts[0] == "memory":
            identity = _text(parts[1], maximum=200)
            if parts[2] == "update":
                _object(body, {"content"})
                result = self.context_memory.update(
                    identity, _text(body.get("content"), maximum=200000, empty=True)
                )
                if result is None:
                    raise ValueError("Memory does not exist")
                return result
            if parts[2] == "delete":
                _object(body, set())
                return {"deleted": self.context_memory.delete(identity)}
        if len(parts) == 3 and parts[0] == "snapshots" and parts[2] == "restore":
            _object(body, set())
            with self._harness_idle():
                return self.file_snapshots.restore(parts[1])
        if len(parts) == 3 and parts[0] == "approvals" and parts[2] == "respond":
            _object(body, {"approved"})
            approved = body.get("approved")
            if type(approved) is not bool:
                raise ValueError("Approval must be a boolean")
            self.approvals.respond(_text(parts[1], maximum=100), approved)
            return {"id": parts[1], "status": "approved" if approved else "denied"}
        return None

"""Bounded local diagnostics and narrowly scoped operator repairs.

No credentials, model output, process command lines or raw errors leave here.
Opening diagnostics does not run executables, discover files, or contact providers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit

from local_coder.engine_discovery import canonical_workspace, connection_directory_lock
from local_coder.inference import validate_model
from local_coder.inference_discovery import inspect_runtime
from local_coder.statedir import state_path


def _unlinked_path(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Linked diagnostic paths are unsupported")
    return path


def _owned(info):
    return os.name == "nt" or info.st_uid == os.getuid()


def descriptor_path(root):
    directory = Path(os.environ.get("LOCAL_CODER_CONNECTION_DIR", Path.home() / ".local-coder/connections"))
    if not directory.is_absolute():
        raise ValueError("Invalid engine discovery directory")
    return directory / (hashlib.sha256(canonical_workspace(root).encode()).hexdigest() + ".json")


def _descriptor(root):
    path = _unlinked_path(descriptor_path(root))
    parent = path.parent.stat()
    if not _owned(parent) or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ValueError("Engine discovery directory is not private")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not _owned(info) or info.st_size > 16384 or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Invalid private engine descriptor")
        body = json.loads(stream.read(16385))
    if not isinstance(body, dict) or body.get("workspace") != canonical_workspace(root):
        raise ValueError("Engine descriptor belongs to another workspace")
    pid = body.get("pid")
    if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
        raise ValueError("Invalid engine PID")
    origin = urlsplit(body.get("origin", ""))
    if origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "::1", "localhost"} or origin.username or origin.password or origin.query or origin.fragment or origin.path not in {"", "/"} or not origin.port:
        raise ValueError("Engine origin must be a literal loopback endpoint")
    return path, info, pid, origin.port


def pid_state(pid):
    if os.name == "nt":
        return "unknown"
    try:
        os.kill(pid, 0)
        return "alive"
    except ProcessLookupError:
        return "dead"
    except (PermissionError, OSError):
        return "unknown"


def engine_diagnostic(root):
    try:
        _, _, pid, port = _descriptor(root)
        state = pid_state(pid)
        return {"status": "ok" if state == "alive" else "warning", "descriptor": "valid",
                "pid_state": state, "loopback": True, "port": port,
                "connection_probe": "not_performed", "summary": "Private loopback engine descriptor; process " + state}
    except FileNotFoundError:
        return {"status": "warning", "descriptor": "missing", "summary": "No engine discovery descriptor"}
    except Exception:
        return {"status": "warning", "descriptor": "invalid", "summary": "Engine discovery descriptor cannot be safely inspected"}


def state_diagnostic(root):
    if os.name == "nt":
        return {"status": "unsupported", "summary": "POSIX private-state permission checks are unavailable"}
    try:
        target = _unlinked_path(Path(root) / ".local-coder")
        if not target.exists():
            return {"status": "warning", "repairable": True, "summary": "Private project state directory is missing"}
        info = target.stat()
        owned = target.is_dir() and _owned(info)
        private = owned and stat.S_IMODE(info.st_mode) == 0o700
        return {"status": "ok" if private else "warning", "repairable": bool(owned and not private),
                "summary": "Project state directory is private" if private else "Project state directory needs attention"}
    except Exception:
        return {"status": "warning", "repairable": False, "summary": "Project state directory cannot be safely inspected"}


def ide_diagnostic():
    name = "Local Coder IDE.exe" if os.name == "nt" else "Local Coder IDE.app/Contents/MacOS/Local Coder IDE" if sys.platform == "darwin" else "local-coder-ide"
    selected = os.environ.get("LOCAL_CODER_IDE_EXECUTABLE")
    candidates = [Path(selected)] if selected else [
        Path(sys.executable).parent.parent / "ide" / name,
        Path.home() / ".local/opt/local-coder/current/ide" / name,
    ]
    executable = next((path for path in candidates if path.is_absolute() and path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))), None)
    result = {"status": "ok" if executable else "warning", "installed": executable is not None,
              "summary": "Installed IDE executable found" if executable else "Installed IDE executable not found in configured locations"}
    if not sys.platform.startswith("linux"):
        result["sandbox"] = {"status": "unsupported", "summary": "Linux setuid sandbox check does not apply on this platform"}
    elif executable is None:
        result["sandbox"] = {"status": "unknown", "summary": "Select an installed IDE to inspect its sandbox helper"}
    else:
        try:
            helper = executable.resolve().parent / "chrome-sandbox"
            info = helper.lstat()
            valid = stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o4755
            result["sandbox"] = {"status": "ok" if valid else "warning", "root_owned_4755": valid,
                                 "summary": "Sandbox helper verified" if valid else "Sandbox helper requires administrator review"}
        except OSError:
            result["sandbox"] = {"status": "warning", "root_owned_4755": False, "summary": "Sandbox helper not available"}
    return result


def diagnose(root, inference=None, provider=None, discovery=None):
    repairs = []
    runtime = {"status": "unknown", "summary": "Local runtime manager unavailable"}
    if inference is not None:
        try:
            current = inference.status()
            state = current.get("state")
            if state not in {"running", "stopped", "starting", "stopping", "error", "installing"}:
                state = "unknown"
            settings = current.get("settings", {})
            runtime = {"status": "ok" if current.get("running") else "warning", "state": state,
                       "summary": "Local inference runtime " + state}
            selected = settings.get("runtime_path")
            try:
                executable = inference._runtime(selected or "")
                runtime["executable_valid"] = bool(inspect_runtime(executable).get("valid"))
            except Exception:
                runtime["executable_valid"] = False
            for field in ("model_path", "draft_model_path"):
                runtime[field + "_selected"] = bool(settings.get(field))
                try:
                    runtime[field + "_valid"] = bool(validate_model(settings[field])) if settings.get(field) else None
                except Exception:
                    runtime[field + "_valid"] = False
            if state == "error" and not current.get("running"):
                repairs.append({"action": "reset_failed_runtime", "label": "Reset failed local runtime"})
        except Exception:
            runtime = {"status": "warning", "summary": "Local runtime status unavailable"}
    provider_status = {"status": "warning", "configured": provider is not None, "model_selected": False, "endpoint_configured": False, "key_present": False}
    if provider is not None:
        try:
            provider_status.update(model_selected=bool(provider.model_id), endpoint_configured=bool(provider.base_url), key_present=bool(provider.resolve_api_key()))
            provider_status["status"] = "ok" if provider_status["model_selected"] and provider_status["endpoint_configured"] else "warning"
        except Exception:
            pass
    provider_status["summary"] = "Provider configuration presence only; no remote connection check"
    engine = engine_diagnostic(root)
    if engine.get("pid_state") == "dead":
        repairs.append({"action": "clear_stale_engine_descriptor", "label": "Clear stale engine descriptor"})
    private = state_diagnostic(root)
    if private.get("repairable"):
        repairs.append({"action": "ensure_private_project_state", "label": "Make project state directory private"})
    discovery_status = {"status": "unknown", "summary": "Runtime discovery has not been inspected"}
    if discovery is not None:
        try:
            status = discovery.status()
            discovery_status = {"status": "ok", "summary": "Existing discovery results only; no scan started",
                                "runtime_candidates": min(len(status.get("runtimes", [])), 10000),
                                "model_candidates": min(len(status.get("models", [])), 10000)}
        except Exception:
            pass
    return {"checks": {"runtime": runtime, "provider": provider_status, "engine": engine,
                       "ide": ide_diagnostic(), "private_state": private, "discovery": discovery_status},
            "repairs": repairs, "network_probes": False}


def repair(root, action, inference=None):
    if action == "reset_failed_runtime":
        current = inference.status() if inference else {}
        if current.get("state") != "error" or current.get("running"):
            raise ValueError("Only a failed local runtime can be reset")
        inference.stop()
        return inference.status().get("state") == "stopped"
    if action == "clear_stale_engine_descriptor":
        # Re-read under the SAME advisory lock used by publishers. The earlier
        # diagnostic is only a hint; a fresh engine may have published since it.
        with connection_directory_lock(descriptor_path(root).parent):
            path, original, pid, _ = _descriptor(root)
            if pid_state(pid) != "dead":
                raise ValueError("Engine descriptor PID is active or unknown")
            latest = path.lstat()
            if (latest.st_dev, latest.st_ino, latest.st_mtime_ns, latest.st_nlink) != (original.st_dev, original.st_ino, original.st_mtime_ns, 1):
                raise ValueError("Engine descriptor changed during verification")
            path.unlink()
            return not path.exists()
    if action == "ensure_private_project_state":
        before = state_diagnostic(root)
        if not before.get("repairable"):
            raise ValueError("Project state cannot be repaired by this action")
        # The existing safe state helper rejects redirected project state paths.
        state_path(root, ".local-coder/doctor-check")
        target = _unlinked_path(Path(root) / ".local-coder")
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or not _owned(info):
                raise ValueError("Project state must belong to the current user")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        return state_diagnostic(root)["status"] == "ok"
    raise ValueError("Unsupported Doctor repair")

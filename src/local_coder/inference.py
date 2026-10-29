"""App-owned llama.cpp lifecycle. Never downloads a model or attaches to a PID.

Only endpoint_config() reveals the per-run credential; status() is UI safe.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

import httpx
from local_coder.inference_discovery import open_regular_candidate


DEFAULTS = {
    "runtime_path": "", "model_path": "", "draft_model_path": "",
    "context_size": 8192, "gpu_layers": 0, "threads": max(1, min(8, os.cpu_count() or 1)),
    "flash_attention": "auto", "prompt_cache": True, "cache_ram_mb": 512,
    "prefetch": True, "prefetch_mb": 256, "startup_timeout": 180,
}


def _guard_storage_directory(path: Path):
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise ValueError("Inference storage cannot use linked directories.")


def validate_model(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Choose an absolute local GGUF file path.")
    path = path.resolve(strict=True)
    if path.suffix.lower() != ".gguf" or not path.is_file() or path.stat().st_size < 24:
        raise ValueError("Model must be a readable, nonempty GGUF file (at least 24 bytes).")
    with open_regular_candidate(path) as (stream, _info):
        if stream.read(4) != b"GGUF":
            raise ValueError("Selected file does not have a GGUF header.")
    return path


def _terminate(proc: subprocess.Popen) -> None:
    """Kill the group even when the leader already exited; reap our child."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=8)


def _process_options() -> dict:
    return ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
            else {"start_new_session": True})


class EmbeddedInferenceManager:
    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(state_dir)
        _guard_storage_directory(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._operation = threading.RLock()
        self._cancel = threading.Event()
        self._worker = None
        self._installer = None
        self._installation = {"state": "idle"}
        self._prefetch_worker = None
        self._prefetch_cancel = threading.Event()
        self._proc = None
        self._probe = None
        self._endpoint = None
        self._key = None
        self._settings = dict(DEFAULTS)
        settings_error = None
        try:
            with open_regular_candidate(self.state_dir / "inference.json") as (stream, info):
                if info.st_nlink != 1 or info.st_size > 65536:
                    raise ValueError("Invalid inference settings file.")
                raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Inference settings exceed their size limit.")
            _guard_storage_directory(self.state_dir)
            saved = json.loads(raw)
            self._settings = self._validated(saved, validate_paths=False)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, RecursionError):
            settings_error = "Saved inference settings were ignored because the file was unsafe or invalid."
        self._state = "stopped"
        self._error = settings_error
        self._capabilities = {}
        self._profile = {}
        self._prefetch = {"state": "idle", "bytes_read": 0, "files": 0}

    def _validated(self, update, *, validate_paths=True):
        if not isinstance(update, dict):
            raise ValueError("Inference settings must be an object.")
        unknown = set(update) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown inference settings: {', '.join(sorted(unknown))}")
        result = dict(self._settings)
        result.update(update)
        for field, lower, upper in (("context_size", 512, 1048576), ("gpu_layers", -1, 999),
                                    ("threads", 1, 1024), ("prefetch_mb", 0, 1024),
                                    ("cache_ram_mb", 0, 4096), ("startup_timeout", 1, 1800)):
            value = result[field]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{field} must be an integer between {lower} and {upper}.")
        for field in ("prompt_cache", "prefetch"):
            if type(result[field]) is not bool:
                raise ValueError(f"{field} must be a boolean.")
        if result["flash_attention"] not in ("auto", "on", "off"):
            raise ValueError("flash_attention must be auto, on, or off.")
        for field in ("runtime_path", "model_path", "draft_model_path"):
            if not isinstance(result[field], str):
                raise ValueError(f"{field} must be a path string.")
            if validate_paths and result[field]:
                if field == "runtime_path":
                    result[field] = str(self._runtime(result[field]))
                else:
                    result[field] = str(validate_model(result[field]))
        if result["draft_model_path"] and result["draft_model_path"] == result["model_path"]:
            raise ValueError("Draft and target models must be different GGUF files.")
        return result

    def _runtime(self, selected="") -> Path:
        name = "llama-server.exe" if os.name == "nt" else "llama-server"
        bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "runtime" / name
        candidates = [Path(selected)] if selected else [self.state_dir / "runtime" / name, bundled]
        if not selected and shutil.which(name):
            candidates.append(Path(shutil.which(name)))
        for candidate in candidates:
            if not candidate.is_absolute():
                raise ValueError("Runtime path must be absolute.")
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate.resolve()
        raise ValueError("llama-server is not installed. Select an official llama.cpp runtime or install one.")

    def status(self) -> dict:
        with self._lock:
            if self._state == "running" and self._proc is not None and self._proc.poll() is not None:
                self._state = "error"
                self._error = f"llama-server exited with code {self._proc.returncode}."
                self._endpoint = self._key = None
            return copy.deepcopy({"state": self._state, "status": self._state,
                "running": self._state == "running", "ready": self._state == "running",
                "model_path": self._settings["model_path"],
                "draft_model_path": self._settings["draft_model_path"],
                "settings": self._settings, "installation": self._installation, "capabilities": self._capabilities,
                "prefetch": self._prefetch, "profiling": self._profile, "error": self._error})

    def configure(self, settings: dict) -> dict:
        with self._operation:
            with self._lock:
                if self._state in ("starting", "running", "stopping", "installing"):
                    raise ValueError("Stop inference before changing its settings.")
                selected = self._validated(settings)
                self._save_settings(selected)
                self._settings = selected
            self._start_prefetch()
            return self.status()

    def _save_settings(self, settings):
        target = self.state_dir / "inference.json"
        temporary = None

        def guard():
            _guard_storage_directory(self.state_dir)
            try:
                info = target.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Inference settings must be an ordinary file.")

        guard()
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.state_dir,
                                             prefix=".inference-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(settings, output, indent=2, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            guard()
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _cancel_prefetch(self):
        self._prefetch_cancel.set()
        if self._prefetch_worker and self._prefetch_worker.is_alive():
            self._prefetch_worker.join(timeout=5)
            if self._prefetch_worker.is_alive():
                raise RuntimeError("Model prefetch has not stopped; try again after storage responds.")

    def _start_prefetch(self):
        self._cancel_prefetch()
        self._prefetch_cancel = threading.Event()
        settings = dict(self._settings)
        cancel = self._prefetch_cancel
        with self._lock:
            self._prefetch = {"state": "idle", "bytes_read": 0, "files": 0,
                              "limit_bytes": settings["prefetch_mb"] * 1048576}
        if not settings["prefetch"] or not settings["model_path"] or not settings["prefetch_mb"]:
            return
        self._prefetch_worker = threading.Thread(target=self._warm_files,
            args=(settings, cancel), name="inference-prefetch", daemon=True)
        self._prefetch_worker.start()

    def _warm_files(self, settings, cancel):
        started = time.monotonic()
        remaining = settings["prefetch_mb"] * 1048576
        paths = [settings["model_path"], settings["draft_model_path"]]
        with self._lock:
            self._prefetch["state"] = "reading"
        try:
            # Split the bound across selected target/draft, using a reusable 1 MiB buffer.
            paths = [validate_model(p) for p in paths if p]
            budget = remaining // len(paths)
            buffer = bytearray(min(1048576, budget))
            for path in paths:
                read = 0
                with path.open("rb", buffering=0) as stream:
                    while read < budget and not cancel.is_set():
                        count = stream.readinto(memoryview(buffer)[:min(len(buffer), budget-read)])
                        if not count:
                            break
                        read += count
                        with self._lock:
                            self._prefetch["bytes_read"] += count
                with self._lock:
                    self._prefetch["files"] += 1
            with self._lock:
                self._prefetch["state"] = "cancelled" if cancel.is_set() else "complete"
        except (OSError, ValueError) as exc:
            with self._lock:
                self._prefetch.update(state="error", error=str(exc))
        finally:
            with self._lock:
                self._prefetch["elapsed_seconds"] = round(time.monotonic()-started, 3)

    def start(self, settings: dict | None = None) -> dict:
        with self._operation:
            if self._worker and self._worker.is_alive():
                return self.status()
            if self.status()["running"]:
                return self.status()
            if self._installer and self._installer.is_alive():
                raise ValueError("Wait for the runtime installation to finish.")
            if settings is not None:
                self.configure(settings)
            selected = self._validated({})
            selected["runtime_path"] = str(self._runtime(selected["runtime_path"]))
            if not selected["model_path"]:
                raise ValueError("Choose a local GGUF model first.")
            self._cancel.clear()
            if self._prefetch["state"] in ("idle", "cancelled", "error"):
                self._start_prefetch()
            with self._lock:
                self._state, self._error = "starting", None
                self._profile = {"started_at": time.time(), "speedup_measured": False}
            self._worker = threading.Thread(target=self._run, args=(selected,),
                                            name="embedded-inference", daemon=True)
            self._worker.start()
            return self.status()

    def _environment(self, runtime):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LLAMA_") and k not in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")}
        directory = str(Path(runtime).parent)
        # Keep official distribution's adjacent shared libraries discoverable.
        env["PATH"] = directory + os.pathsep + env.get("PATH", "")
        if sys.platform.startswith("linux"):
            env["LD_LIBRARY_PATH"] = directory + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        return env

    def _help(self, runtime, env):
        if self._cancel.is_set():
            raise RuntimeError("Runtime capability probe cancelled.")
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen([runtime, "--help"], stdout=output, stderr=subprocess.STDOUT,
                                    env=env, **_process_options())
            with self._lock:
                self._probe = proc
            try:
                deadline = time.monotonic() + 8
                while proc.poll() is None:
                    if self._cancel.wait(.05) or time.monotonic() >= deadline:
                        raise RuntimeError("Runtime capability probe cancelled or timed out.")
                output.seek(0)
                help_text = output.read(2 * 1048576).decode("utf-8", errors="replace")
                if proc.returncode != 0:
                    raise ValueError("Runtime --help failed. Check platform and adjacent runtime libraries.")
                return help_text
            finally:
                _terminate(proc)
                with self._lock:
                    self._probe = None

    def _command(self, settings, help_text, port, key_file):
        flags = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", help_text))
        required = {"--model", "--host", "--port", "--ctx-size"}
        if not required <= flags or not ({"--api-key-file", "--api-key"} & flags):
            raise ValueError("Runtime lacks required model, loopback, context, or authentication flags.")
        command = [settings["runtime_path"], "--model", settings["model_path"],
                   "--host", "127.0.0.1", "--port", str(port), "--ctx-size", str(settings["context_size"])]
        if "--api-key-file" in flags:
            command += ["--api-key-file", str(key_file)]
        else:
            # Older builds support env authentication, avoiding a secret in argv.
            pass
        capabilities = {"speculative_decoding": False, "flash_attention": False,
                        "prompt_cache": False, "notes": [], "flags": sorted(flags)}
        for flag, setting in (("--threads", "threads"), ("--gpu-layers", "gpu_layers")):
            if flag in flags:
                command += [flag, str(settings[setting])]
            elif setting == "gpu_layers" and settings[setting] != 0:
                raise ValueError("Selected runtime does not support the requested GPU layer setting.")
        if "--parallel" in flags:
            command += ["--parallel", "1"]
        if "--alias" in flags:
            command += ["--alias", "app-local"]
        if "--flash-attn" in flags:
            # Do not pass enum values to old boolean-only versions of this option.
            line = next((line for line in help_text.splitlines() if "--flash-attn" in line), "")
            if "auto" in line and "off" in line:
                command += ["--flash-attn", settings["flash_attention"]]
                capabilities["flash_attention"] = settings["flash_attention"] != "off"
            else:
                capabilities["notes"].append("Flash attention setting unavailable in this runtime version.")
        if "--cache-ram" in flags:
            command += ["--cache-ram", str(settings["cache_ram_mb"] if settings["prompt_cache"] else 0)]
            capabilities["prompt_cache"] = settings["prompt_cache"] and settings["cache_ram_mb"] > 0
        if settings["prompt_cache"] and "--cache-reuse" in flags:
            command += ["--cache-reuse", "256"]
            capabilities["prompt_cache"] = True
        if not capabilities["prompt_cache"]:
            capabilities["notes"].append("Server prompt cache flags unavailable or disabled; request cache_prompt may still reuse its active slot.")
        if settings["draft_model_path"]:
            draft = next((f for f in ("--spec-draft-model", "--model-draft") if f in flags), None)
            if draft:
                command += [draft, settings["draft_model_path"]]
                if "--spec-type" in flags:
                    if "draft-simple" not in help_text:
                        raise ValueError("Runtime supports --spec-type but not draft-simple decoding.")
                    command += ["--spec-type", "draft-simple"]
                capabilities["speculative_decoding"] = True
                capabilities["notes"].append("Draft compatibility and speed depend on model pair; no speed gain has been measured.")
            else:
                capabilities["notes"].append("Draft decoding disabled: this runtime lacks a draft model flag.")
        with self._lock:
            self._capabilities = capabilities
        return command, flags

    def _run(self, settings):
        started = time.monotonic()
        proc = None
        key_file = None
        try:
            env = self._environment(settings["runtime_path"])
            help_text = self._help(settings["runtime_path"], env)
            if self._cancel.is_set():
                return
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            key = secrets.token_urlsafe(32)
            fd, name = tempfile.mkstemp(prefix="inference-key-", dir=self.state_dir)
            key_file = Path(name)
            with os.fdopen(fd, "w") as output:
                output.write(key + "\n")
            command, flags = self._command(settings, help_text, port, key_file)
            if "--api-key-file" not in flags:
                env["LLAMA_API_KEY"] = key
            if self._cancel.is_set():
                return
            # Logs can contain server-echoed credentials. Discard server output;
            # report exit code and safe, application-generated errors instead.
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, env=env, **_process_options())
            with self._lock:
                self._proc = proc
            endpoint = f"http://127.0.0.1:{port}/v1"
            deadline = time.monotonic() + settings["startup_timeout"]
            with httpx.Client(timeout=.5, trust_env=False) as client:
                while not self._cancel.is_set():
                    if proc.poll() is not None:
                        raise RuntimeError(f"llama-server exited with code {proc.returncode}; check model compatibility, available memory and runtime libraries.")
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Model startup timed out; try a smaller model/context or a longer startup timeout.")
                    try:
                        health = client.get(f"http://127.0.0.1:{port}/health",
                                            headers={"Authorization": f"Bearer {key}"})
                        authenticated = client.get(endpoint + "/models", headers={"Authorization": f"Bearer {key}"})
                        if health.status_code == 200 and authenticated.status_code == 200:
                            if client.get(endpoint + "/models").status_code not in (401, 403):
                                raise RuntimeError("Runtime did not enforce authentication; stopped for safety.")
                            with self._lock:
                                if self._cancel.is_set():
                                    return
                                self._endpoint, self._key = endpoint, key
                                self._state = "running"
                                self._profile["startup_seconds"] = round(time.monotonic()-started, 3)
                            break
                    except httpx.HTTPError:
                        pass
                    self._cancel.wait(.2)
            while not self._cancel.wait(.2):
                if proc.poll() is not None:
                    raise RuntimeError(f"llama-server exited with code {proc.returncode}.")
        except Exception as exc:
            with self._lock:
                if not self._cancel.is_set():
                    self._state, self._error = "error", str(exc)
        finally:
            if proc is not None:
                try:
                    _terminate(proc)
                except (OSError, subprocess.SubprocessError) as exc:
                    with self._lock:
                        self._state, self._error = "error", f"Runtime shutdown failed: {type(exc).__name__}"
            if key_file:
                key_file.unlink(missing_ok=True)
            with self._lock:
                self._endpoint = self._key = None
                self._proc = None
                if self._cancel.is_set() and self._state != "error":
                    self._state = "stopped"

    def stop(self) -> dict:
        with self._operation:
            self._cancel.set()
            self._cancel_prefetch()
            with self._lock:
                if self._state != "error":
                    self._state = "stopping"
                self._endpoint = self._key = None
            if self._installer and self._installer.is_alive():
                self._installer.join(timeout=20)
                if self._installer.is_alive():
                    with self._lock:
                        self._state, self._error = "error", "Runtime download cancellation is still pending."
                    return self.status()
            if self._worker and self._worker.is_alive():
                self._worker.join(timeout=20)
                if self._worker.is_alive():
                    with self._lock:
                        self._state, self._error = "error", "Runtime shutdown is still pending."
                    return self.status()
            with self._lock:
                self._state = "stopped"
            return self.status()

    def endpoint_config(self) -> dict | None:
        with self._lock:
            if not self.status()["ready"] or not self._endpoint:
                return None
            return {"base_url": self._endpoint, "api_key": self._key,
                    "model": "app-local" if "--alias" in self._capabilities.get("flags", []) else Path(self._settings["model_path"]).name}

    close = stop

    def install_runtime(self, asset_name: str | None = None) -> dict:
        """Explicit opt-in installation of a checksum verified official CPU runtime.

        Runs asynchronously. Never downloads GGUF files. An optional exact release
        asset name allows advanced selection, still restricted to official assets.
        """
        with self._operation:
            if self._state in ("starting", "running", "stopping"):
                raise ValueError("Stop inference before installing a runtime.")
            if self._installer and self._installer.is_alive():
                return self.status()
            self._cancel.clear()
            with self._lock:
                self._state, self._error = "installing", None
                self._installation = {"state": "resolving", "bytes_downloaded": 0}
            self._installer = threading.Thread(target=self._install, args=(asset_name,),
                                               name="inference-installer", daemon=True)
            self._installer.start()
            return self.status()

    def _select_asset(self, releases, requested=None):
        machine = platform.machine().lower()
        architecture = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("amd64", "x86_64") else ""
        if not architecture:
            raise ValueError("No automatic runtime for this CPU architecture; select a locally built llama-server.")
        system = "win" if os.name == "nt" else "macos" if sys.platform == "darwin" else "ubuntu" if sys.platform.startswith("linux") else ""
        if not system:
            raise ValueError("No automatic runtime for this platform; select a locally built llama-server.")
        for release in releases:
            if release.get("draft") or release.get("prerelease"):
                continue
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                lower = name.lower()
                if requested:
                    match = name == requested
                else:
                    match = (system in lower and architecture in lower and
                             not any(x in lower for x in ("cuda", "vulkan", "sycl", "rocm", "opencl")) and
                             (system != "win" or "cpu" in lower))
                if match and lower.endswith((".zip", ".tar.gz", ".tgz")):
                    digest = asset.get("digest", "")
                    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                        continue
                    url = asset.get("browser_download_url", "")
                    if not url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/"):
                        continue
                    return asset
        raise ValueError("No checksum-verified official runtime asset matched this platform. Download/build llama.cpp from ggml-org/llama.cpp and select llama-server manually.")

    def _extract_runtime(self, archive: Path, destination: Path):
        """Extract regular files only, rejecting traversal, links and zip bombs."""
        _guard_storage_directory(destination)
        limit = 4 * 1024 ** 3
        total = 0
        count = 0
        links = []

        def target(name, size):
            nonlocal total, count
            if "\\" in name or ":" in name or name.startswith("/"):
                raise ValueError("Unsafe runtime archive path.")
            relative = Path(name)
            if ".." in relative.parts:
                raise ValueError("Unsafe runtime archive path.")
            result = destination / relative
            total += size
            count += 1
            if total > limit or count > 10000:
                raise ValueError("Runtime archive exceeds extraction bounds.")
            _guard_storage_directory(result.parent)
            result.parent.mkdir(parents=True, exist_ok=True)
            _guard_storage_directory(result.parent)
            if result.is_symlink():
                raise ValueError("Unsafe runtime archive linked destination.")
            return result

        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if self._cancel.is_set():
                        raise RuntimeError("Runtime installation cancelled.")
                    if member.is_dir():
                        continue
                    mode = member.external_attr >> 16
                    if mode & 0o170000 == 0o120000:
                        if member.file_size > 4096:
                            raise ValueError("Unsafe runtime archive symbolic link.")
                        path = target(member.filename, 0)
                        links.append((path, bundle.read(member).decode("utf-8"), False))
                        continue
                    path = target(member.filename, member.file_size)
                    with bundle.open(member) as source, path.open("wb") as output:
                        shutil.copyfileobj(source, output, 1048576)
                    path.chmod(0o755 if path.name == "llama-server" else 0o644)
        else:
            with tarfile.open(archive, "r:gz") as bundle:
                for member in bundle:
                    if self._cancel.is_set():
                        raise RuntimeError("Runtime installation cancelled.")
                    if member.isdir():
                        continue
                    if member.issym() or member.islnk():
                        links.append((target(member.name, 0), member.linkname, member.islnk()))
                        continue
                    if not member.isfile():
                        raise ValueError("Runtime archive contains a special file.")
                    path = target(member.name, member.size)
                    with bundle.extractfile(member) as source, path.open("wb") as output:
                        shutil.copyfileobj(source, output, 1048576)
                    path.chmod(0o755 if path.name == "llama-server" else 0o644)
        # Official distributions can contain library aliases. Materialize only
        # internal regular-file targets, never create archive-controlled symlinks.
        for path, link, hard in links:
            if Path(link).is_absolute() or "\\" in link or ":" in link:
                raise ValueError("Unsafe runtime archive symbolic link.")
            source = ((destination if hard else path.parent) / link).resolve()
            if not source.is_relative_to(destination.resolve()) or not source.is_file():
                raise ValueError("Unsafe runtime archive symbolic link target.")
            total += source.stat().st_size
            if total > limit:
                raise ValueError("Runtime archive exceeds extraction bounds.")
            shutil.copyfile(source, path)
            path.chmod(0o755 if path.name == "llama-server" else 0o644)

    def _install(self, requested):
        installed = None
        published = False
        try:
            _guard_storage_directory(self.state_dir)
            with httpx.Client(timeout=httpx.Timeout(10, read=5), follow_redirects=True,
                              headers={"User-Agent": "local-coder-runtime-installer"}) as client:
                response = client.get("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30")
                response.raise_for_status()
                asset = self._select_asset(response.json(), requested)
                maximum = 2 * 1024 ** 3
                if asset.get("size", maximum+1) > maximum:
                    raise ValueError("Runtime asset exceeds the 2 GiB download limit.")
                _guard_storage_directory(self.state_dir)
                with tempfile.TemporaryDirectory(prefix="runtime-install-", dir=self.state_dir) as temporary:
                    staging = Path(temporary)
                    archive = staging / "runtime.archive"
                    digest = hashlib.sha256()
                    with self._lock:
                        self._installation.update(state="downloading", asset=asset["name"],
                                                  total_bytes=asset["size"])
                    count = 0
                    # The release digest covers the exact archive bytes. Some CDNs
                    # label those bytes Content-Encoding: gzip/deflate; iter_bytes
                    # would decode them again and can raise zlib's "incorrect
                    # header check" before archive verification. Request identity
                    # and preserve the wire bytes; the official digest still gates
                    # extraction and executable probing.
                    with client.stream("GET", asset["browser_download_url"], headers={"Accept-Encoding": "identity"}) as download:
                        download.raise_for_status()
                        with archive.open("wb") as output:
                            chunks = ([download.content] if download.is_stream_consumed else download.iter_raw(1048576))
                            for chunk in chunks:
                                if self._cancel.is_set():
                                    raise RuntimeError("Runtime installation cancelled.")
                                count += len(chunk)
                                if count > maximum or count > asset["size"]:
                                    raise ValueError("Runtime download exceeded the declared size.")
                                digest.update(chunk)
                                output.write(chunk)
                                with self._lock:
                                    self._installation["bytes_downloaded"] = count
                    if count != asset["size"] or digest.hexdigest() != asset["digest"].split(":", 1)[1]:
                        raise ValueError("Official runtime checksum verification failed.")
                    with self._lock:
                        self._installation["state"] = "extracting"
                    extracted = staging / "files"
                    extracted.mkdir()
                    self._extract_runtime(archive, extracted)
                    name = "llama-server.exe" if os.name == "nt" else "llama-server"
                    matches = list(extracted.rglob(name))
                    if len(matches) != 1:
                        raise ValueError("Runtime archive must contain exactly one llama-server executable.")
                    relative = matches[0].relative_to(extracted)
                    # Validate the downloaded executable before making it the selection.
                    if self._cancel.is_set():
                        raise RuntimeError("Runtime installation cancelled.")
                    help_text = self._help(str(matches[0]), self._environment(matches[0]))
                    if "--model" not in help_text or "--host" not in help_text:
                        raise ValueError("Downloaded runtime does not support llama-server flags.")
                    if self._cancel.is_set():
                        raise RuntimeError("Runtime installation cancelled.")
                    destination = self.state_dir / ("runtime-" + secrets.token_hex(6))
                    _guard_storage_directory(self.state_dir)
                    if destination.exists() or destination.is_symlink():
                        raise ValueError("Runtime installation destination already exists; retry installation.")
                    extracted.rename(destination)
                    installed = destination
                    with self._lock:
                        selected = {**self._settings, "runtime_path": str(installed / relative)}
                        self._save_settings(selected)
                        self._settings = selected
                        published = True
                        self._installation.update(state="complete", checksum_verified=True)
                        self._state = "stopped"
        except Exception as exc:
            with self._lock:
                self._installation["state"] = "cancelled" if self._cancel.is_set() else "error"
                self._state = "stopped" if self._cancel.is_set() else "error"
                self._error = None if self._cancel.is_set() else str(exc)
        finally:
            if installed is not None and not published:
                try:
                    shutil.rmtree(installed)
                except OSError:
                    with self._lock:
                        self._installation["cleanup_pending"] = True

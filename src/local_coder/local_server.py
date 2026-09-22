"""Manage this machine's local inference server(s) for local-coding-agent.

config/config.yaml points at http://localhost:8090/v1 (the big model) and
http://localhost:8091/v1 (a small, genuinely separate draft model -- see
README.md's "Local model server" section for why that has to be a
different process, not just a different config entry pointing at the same
server). This module starts/stops/checks both as detached background
processes and lets you pick which GGUF quantization loads for the big
model.

This is intentionally specific to this machine's AI2 project (~/AI2,
overridable via LOCAL_CODER_AI2_DIR): if you don't have that project, run
your own server however you like and just point config.yaml at it -- none
of this module is required to use local-coding-agent.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

AI2_DIR = os.environ.get("LOCAL_CODER_AI2_DIR", "/home/everett/AI2")
STATE_DIR = Path.home() / ".local-coder" / "local_server"
BIG_MODEL_PORT = 8090
DRAFT_MODEL_PORT = 8091
DRAFT_MODEL_PATH = os.path.join(AI2_DIR, "Model Training", "Qwen2.5-Coder-0.5B-Instruct-Q8_0.gguf")
LLAMA_SERVER_BIN = (
    "/home/everett/.lmstudio/extensions/backends/"
    "llama.cpp-linux-x86_64-nvidia-cuda12-avx2-2.37.0/llama-server"
)
LLAMA_SERVER_LD_PATH = (
    "/home/everett/.lmstudio/extensions/backends/vendor/linux-llama-cuda12-vendor-v1:"
    "/home/everett/.lmstudio/extensions/backends/llama.cpp-linux-x86_64-nvidia-cuda12-avx2-2.37.0"
)

# name -> (filename under AI2/models/quants/, human note). Matches the
# quants AI2's own config.py knows how to load.
QUANTS: dict[str, tuple[str, str]] = {
    "q2_k": ("Qwen_Qwen3-30B-A3B-Instruct-2507-Q2_K.gguf", "fastest, ~185 tok/s (default)"),
    "iq3_xxs": ("Qwen_Qwen3-30B-A3B-Instruct-2507-IQ3_XXS.gguf", "~113 tok/s"),
    "ud-q3_k_xl": ("Qwen3-30B-A3B-Instruct-2507-UD-Q3_K_XL.gguf", "best quality/speed balance, ~115 tok/s"),
    "ud-iq2_xxs": ("Qwen3-30B-A3B-Instruct-2507-UD-IQ2_XXS.gguf", "smallest file, lower quality"),
}


@dataclass
class ModelInfo:
    name: str
    path: str
    size_gb: float
    note: str


def list_available_models() -> list[ModelInfo]:
    """Quants actually present on disk under AI2/models/quants/, not just
    documented -- a fresh AI2 checkout may only have some of them."""
    quants_dir = Path(AI2_DIR) / "models" / "quants"
    available = []
    for name, (filename, note) in QUANTS.items():
        path = quants_dir / filename
        if path.is_file():
            available.append(ModelInfo(
                name=name, path=str(path),
                size_gb=round(path.stat().st_size / 1e9, 1), note=note,
            ))
    return available


def _pid_file(which: str) -> Path:
    return STATE_DIR / f"{which}.pid"


def _read_pid(which: str) -> int | None:
    path = _pid_file(which)
    if not path.is_file():
        return None
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _health(port: int, timeout: float = 2.0) -> bool:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=timeout)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def status() -> dict:
    """pid/liveness/port-health for both servers, keyed "big" and "draft"."""
    result = {}
    for which, port in (("big", BIG_MODEL_PORT), ("draft", DRAFT_MODEL_PORT)):
        pid = _read_pid(which)
        result[which] = {
            "pid": pid,
            "process_running": pid is not None and _is_running(pid),
            "port": port,
            "healthy": _health(port),
        }
    return result


def start_big_model(quant: str = "q2_k", n_ctx: int = 65536, slot_cache: bool = True) -> dict:
    if quant not in QUANTS:
        raise ValueError(f"Unknown quant {quant!r}; choose one of {', '.join(QUANTS)}")
    existing = _read_pid("big")
    if existing and _is_running(existing):
        return {"status": "already_running", "pid": existing}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["AI2_BIG_MODEL"] = quant
    env["AI2_N_CTX"] = str(n_ctx)
    env["AI2_DIR"] = AI2_DIR
    if slot_cache:
        env["AI2_SLOT_CACHE_DIR"] = str(STATE_DIR / "slot_cache")
    elif "AI2_SLOT_CACHE_DIR" in env:
        del env["AI2_SLOT_CACHE_DIR"]

    log_path = STATE_DIR / "big_model.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "local_coder._local_server_runner"],
        env=env, stdout=log_file, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _pid_file("big").write_text(str(proc.pid))
    return {"status": "started", "pid": proc.pid, "quant": quant, "n_ctx": n_ctx, "log": str(log_path)}


def start_draft_model() -> dict:
    if not os.path.isfile(DRAFT_MODEL_PATH):
        raise FileNotFoundError(f"Draft model not found: {DRAFT_MODEL_PATH}")
    existing = _read_pid("draft")
    if existing and _is_running(existing):
        return {"status": "already_running", "pid": existing}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{LLAMA_SERVER_LD_PATH}:{env.get('LD_LIBRARY_PATH', '')}"
    cmd = [
        # -ngl 0: CPU only, deliberately. The big model's VRAM split
        # (start_big_model) is computed assuming it has the whole 12GB card
        # to itself; putting this 0.5B model on the GPU too leaves only a
        # few hundred MB of headroom, which produced real request-time 500s
        # ("CUDA error: out of memory" during the big model's lazy cuBLAS
        # allocation) under real use. A 0.5B Q8 model is fast enough on CPU
        # that it doesn't need the GPU anyway -- that's the whole point of
        # using something this small as the draft model.
        LLAMA_SERVER_BIN, "-m", DRAFT_MODEL_PATH, "-c", "8192", "-t", "4",
        "--port", str(DRAFT_MODEL_PORT), "-ngl", "0", "-fa", "on", "-np", "1",
    ]
    log_path = STATE_DIR / "draft_model.log"
    log_file = open(log_path, "w")
    proc = subprocess.Popen(cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    _pid_file("draft").write_text(str(proc.pid))
    return {"status": "started", "pid": proc.pid, "log": str(log_path)}


def stop(which: str) -> dict:
    pid = _read_pid(which)
    if not pid or not _is_running(pid):
        _pid_file(which).unlink(missing_ok=True)
        return {"status": "not_running"}
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _is_running(pid):
            break
        time.sleep(0.5)
    else:
        os.kill(pid, signal.SIGKILL)
    _pid_file(which).unlink(missing_ok=True)
    return {"status": "stopped", "pid": pid}


def switch_big_model(quant: str, n_ctx: int = 65536, slot_cache: bool = True) -> dict:
    """Stop the running big-model server (if any) and start it again with a
    different quant -- llama-server can't hot-swap the loaded weights."""
    stop("big")
    return start_big_model(quant=quant, n_ctx=n_ctx, slot_cache=slot_cache)


def wait_healthy(which: str, timeout: float = 180.0) -> bool:
    port = BIG_MODEL_PORT if which == "big" else DRAFT_MODEL_PORT
    start_t = time.time()
    while time.time() - start_t < timeout:
        if _health(port):
            return True
        pid = _read_pid(which)
        if pid and not _is_running(pid):
            return False  # process exited before becoming healthy
        time.sleep(2.0)
    return False


_GPU_FIELDS = (
    "name", "utilization.gpu", "memory.used", "memory.total",
    "temperature.gpu", "power.draw", "power.limit",
)


def gpu_status() -> dict:
    """Live nvidia-smi readout for the UI's GPU usage bar. Returns
    {"available": False} on a machine with no NVIDIA GPU or driver --
    the caller renders that as an empty/hidden bar rather than an error,
    since local-coding-agent works fine on CPU-only or non-NVIDIA setups
    too."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(_GPU_FIELDS)}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"available": False}

    # Multi-GPU machines get one CSV line per card; report each, plus the
    # first as "primary" so a single-GPU UI bar doesn't need to know how
    # many cards there are.
    gpus = []
    for line in proc.stdout.strip().splitlines():
        values = [v.strip() for v in line.split(",")]
        if len(values) != len(_GPU_FIELDS):
            continue
        name, util, mem_used, mem_total, temp, power_draw, power_limit = values
        gpus.append({
            "name": name,
            "utilization_pct": float(util),
            "memory_used_mb": float(mem_used),
            "memory_total_mb": float(mem_total),
            "temperature_c": float(temp),
            "power_draw_w": float(power_draw),
            "power_limit_w": float(power_limit),
        })
    if not gpus:
        return {"available": False}
    return {"available": True, "gpus": gpus}

"""Standalone entry point for the AI2 big-model llama-server, run as a
subprocess by local_server.py (`python3 -m local_coder._local_server_runner`).

Not meant to be imported -- launched with `-m` so AI2_BIG_MODEL (read by
AI2's own config.py at import time) is picked up fresh in a clean process
for every launch, including a quant switch between two calls in the same
local-coding-agent CLI process.

Reads from the environment: AI2_DIR (default /home/everett/AI2),
AI2_BIG_MODEL (quant name, read by AI2's config.py), AI2_N_CTX (context
length, default 65536), AI2_SLOT_CACHE_DIR (optional -- enables
--slot-save-path there; see README.md's "Session cache" section).
"""
import os
import sys
import time

if __name__ != "__main__":
    raise RuntimeError("_local_server_runner is a standalone entry point, not an importable module")

AI2_DIR = os.environ.get("AI2_DIR", "/home/everett/AI2")
sys.path.insert(0, AI2_DIR)

from config import Paths, Runtime  # noqa: E402
from local_engine import BigModelServer  # noqa: E402

n_ctx = int(os.environ.get("AI2_N_CTX", "65536"))
slot_cache_dir = os.environ.get("AI2_SLOT_CACHE_DIR")

paths = Paths()
rt = Runtime(n_ctx=n_ctx)
if slot_cache_dir:
    os.makedirs(slot_cache_dir, exist_ok=True)
    rt = Runtime(n_ctx=n_ctx, spec_args=rt.spec_args + ("--slot-save-path", slot_cache_dir))

print(
    f"[local_server_runner] launching {os.environ.get('AI2_BIG_MODEL', 'q2_k')} "
    f"at n_ctx={n_ctx}, slot_cache_dir={slot_cache_dir!r} ...",
    flush=True,
)
server = BigModelServer(paths, rt)
print(f"READY base_url={server.base_url} n_cpu_moe={server.n_cpu_moe}", flush=True)

try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    pass
finally:
    server.stop()

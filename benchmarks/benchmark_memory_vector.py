"""Reproducible eager Python / streaming Python / streaming Rust comparison.

Run with the release library enabled and PYTHONPATH=src. Each mode runs in its
own process. Python allocation peaks exclude native allocations; the Rust kernel
uses a fixed 256-double stack buffer and never retains input or output pointers.
"""
import json
import math
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc

from local_coder.context.code_index import iter_tokens, tokenize
from local_coder.memory.native import _load, lexical_counts, python_counts


def worker(mode):
    text = (Path(__file__).resolve().parents[1] / "src/local_coder/agents/base.py").read_text()
    if mode == "rust" and _load(os.environ.get("LOCAL_CODER_NATIVE_LIBRARY", "")) is None:
        raise RuntimeError("Build and enable the Rust release library before benchmarking")
    def run():
        raw = (lexical_counts(iter_tokens(text)) if mode == "rust" else
               python_counts(iter_tokens(text) if mode == "streaming_python" else tokenize(text)))
        norm = math.sqrt(sum(value * value for value in raw)) or 1
        return [value / norm for value in raw]
    run()
    durations = []
    for _ in range(10):
        start = time.perf_counter()
        run()
        durations.append((time.perf_counter() - start) * 1000)
    tracemalloc.start()
    vector = run()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"mode": mode, "input_bytes": len(text.encode()), "median_ms": statistics.median(durations),
            "python_peak_bytes": peak, "process_peak_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "vector": vector}


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(json.dumps(worker(sys.argv[1])))
    else:
        results = [json.loads(subprocess.check_output([sys.executable, __file__, mode]))
                   for mode in ("eager_python", "streaming_python", "rust")]
        assert all(row["vector"] == results[0]["vector"] for row in results)
        for row in results:
            row.pop("vector")
        print(json.dumps({"exact_vector_parity": True, "results": results}, indent=2))

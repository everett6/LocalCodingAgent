# In-app local inference

The app starts its own `llama-server` process directly. LM Studio, Ollama,
`~/AI2`, Python subprocess entrypoints, and paid model APIs are not required.
Model weights remain local. No model download occurs when configuring, starting,
or installing the runtime.

Choose an absolute path to a readable `.gguf` model, then install the runtime or
choose your own `llama-server` executable. The explicit installer downloads only
an official [ggml-org/llama.cpp release](https://github.com/ggml-org/llama.cpp/releases)
archive appropriate for the operating system and CPU architecture. It verifies
GitHub's SHA-256 asset digest and declared size, bounds download/extraction, rejects
unsafe archive paths, and probes the executable before saving its location.
If no verified matching asset exists, select a locally built official executable.
Keep its adjacent shared libraries with it. Automatic selection favors CPU
builds; GPU builds may need platform-specific drivers and dependencies. This
repository does not redistribute a llama.cpp binary or model weights.

Start returns immediately while the app loads the selected model in a background
worker. Status reports `starting`, `running`, `error`, or `stopped`; installation
has its own progress counters. The default context is 8192 tokens, with CPU
inference (`gpu_layers: 0`). Raise GPU layers for a suitable GPU-enabled runtime,
or reduce context/model size if memory is insufficient. A model must fit
available hardware; the app cannot make an oversized model fit automatically.

The process listens only on a dynamically selected `127.0.0.1` port and receives
a fresh random API key each run. Readiness verifies both model health and an
authenticated model-list request, and rejects runtimes that accept unauthenticated
model-list requests. llama.cpp's `/health` route itself is public. The ephemeral
credential is returned only to the app's model client, never in public runtime
status or saved configuration. Server stdout/stderr are discarded to avoid
retaining a runtime's echoed credential. Settings persist in the app state
folder. Restarting the app never adopts an unrelated server or saved PID.
Stopping or closing cancels prefetch, stops installation/startup, terminates the
owned process group/tree, and clears the ephemeral key. A pending download/read
can take its bounded network timeout or a storage operation to respond.

The app reads the selected target and optional draft model ahead of loading,
using a reusable 1 MiB buffer and a shared configurable read budget (default
256 MiB, maximum 1024 MiB). This warms operating-system file pages; it does not
reserve the entire model in RAM. Only those selected files are read. Status
reports actual bytes, file count and elapsed time. This is predictive model-file
prefetch, not speculative execution of future user tasks or external requests.
The operating system can evict warmed pages, and an unresponsive filesystem can
delay cancellation of an individual read.

Flags are discovered from the installed executable's `--help` before use. Where
supported, the app enables bounded server prompt caching and KV reuse, sets one
parallel slot, and requests automatic flash attention. Older CLI versions that
cannot express the setting report that limitation. llama.cpp may still maintain
an active request's KV cache independently of the bounded server prompt cache.

An optional *different* draft GGUF enables native speculative decoding when the
runtime advertises a draft-model flag. New CLI versions also require
`--spec-type draft-simple`. The target and draft must use compatible model
architectures/tokenizers; the runtime checks compatibility when loading. Draft
weights consume additional memory. Speculation, cache reuse, flash attention,
and prefetch can help some workloads, but no speed gain is claimed without a
benchmark. Profiling records observed startup time and marks speedup as
unmeasured. Tokens/second and draft acceptance need actual inference measurements.

Implementation contract: `EmbeddedInferenceManager(state_dir)`, `configure(dict)`,
`start(dict | None)`, `stop()`/`close()`, `status()`, and explicit asynchronous
`install_runtime(asset_name=None)`. `endpoint_config()` supplies the private
ready-only `base_url`, `api_key`, and `model` to the provider integration.

See the official [server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
for prompt caching, flash attention, authentication, and speculative options.
Its options evolve; the installed executable's capabilities govern actual usage.

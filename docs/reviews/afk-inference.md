# Embedded inference resilience review

Reviewed 2026-10-29 during the AFK work block. Scope: `inference.py`,
`inference_discovery.py`, and the existing installer, lifecycle, API, and tuning
tests. This is a bounded code review with synthetic fixtures, not a certification
of every local inference or filesystem boundary.

## Proven defects and changes

- **Unsafe settings persistence (private ledger F11).** A pre-existing
  `inference.json.tmp` symlink caused configuration to overwrite an unrelated
  fixture file. Settings now use a unique private temporary file, flush/fsync,
  and atomic replacement; linked/special settings targets and linked storage
  ancestors are rejected. A failed save leaves the selected runtime unchanged in
  both memory and the existing settings file. Failed installation removes only
  its own newly installed directory. A colliding pre-existing directory is never
  treated as owned cleanup.
- **Metadata special-file replacement (F12).** Replacing a checked candidate
  with a FIFO blocked both model and runtime inspectors. Metadata reads now open
  a regular-file descriptor with no-follow/nonblocking flags where supported,
  use descriptor sizes, and compare file identity before/after inspection. The
  same helper protects model-header validation. Discovery still resolves
  intentionally selected model-file symlinks before inspecting their target;
  finding a runtime never executes it or marks it trusted.
- **Unsafe startup settings reads (F13).** A settings FIFO blocked manager
  construction; linked settings loaded unintended file contents. The constructor
  now accepts only an ordinary singly linked settings file, reads at most 64 KiB,
  and uses the descriptor checks above. Invalid/unsafe settings preserve defaults
  and expose a static diagnostic without echoing contents. Missing settings keep
  the normal first-launch behavior. Linked storage ancestors are rejected before
  creating directories, staging downloads, or writing archive members.
- **Cancelled installation still probed its executable.** Cancellation after
  extraction is checked before capability probing, and an already-cancelled
  probe never spawns. No new runtime selection is published after cancellation.
- **Stale, accumulating metadata cache.** Size and restored mtime did not detect
  in-place changes or atomic replacements. Cache identity now includes device,
  inode, size, mtime, and ctime. It keeps one version per canonical path with LRU
  limits of 4096 entries and 4 MiB of serialized record payload. Oversized records
  are returned without caching; cache eviction does not truncate scan results.
  Invalid/nonfinite discovery bounds are rejected.

The existing archive transport fix remains intact: download exact archive wire
bytes with identity encoding, verify the official release digest, and only then
extract and probe. Synthetic gzip/deflate Content-Encoding regressions still pass.

## Verification

83 tests passed in 3.80 seconds:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_inference_persistence.py \
  tests/test_inference_discovery_freshness.py \
  tests/test_inference_metadata_reads.py \
  tests/test_embedded_inference.py \
  tests/test_inference_installer_regressions.py \
  tests/test_inference_discovery.py \
  tests/test_inference_api.py \
  tests/test_inference_tuning.py
```

New regressions were observed failing before their fixes, including the settings
and metadata FIFO hangs. FIFO cases run in bounded child processes which are
killed/reaped on timeout. Persistence tests cover failed atomic replacement,
private file mode, temporary cleanup, special/link targets, collision cleanup,
cancellation, and ordinary settings round-tripping. Discovery tests cover
restored-mtime updates, replacement, cache count/byte limits, and repeated versions.
`compileall` and `git diff --check` passed for the changed sources. Ruff could not
run because it is not installed in the project virtual environment.

No real models were loaded, downloaded, or probed. HTTP responses and archive
payloads were generated locally with MockTransport. The pre-existing lifecycle
tests use a tiny local fixture process, not a real llama.cpp server. No external
hosts were contacted, no paid requests were made, and no system installation or
configuration was changed. No commits or push were performed by this review.

## Limits and follow-up

- This is application validation, not OS isolation against a concurrent process
  with the same user's privileges. Directory guards are portable checks; they
  are not descriptor-relative extraction or an OS sandbox. Windows/macOS were
  not exercised; no-follow/nonblocking availability is platform dependent.
- The cache byte budget measures serialized payload, not exact Python RSS.
  Active discovery result lists retain their existing scan entry/time limits.
- This phase did not change the separate locations manifest parser, inherited
  runtime environment policy, runtime output disk quotas, release metadata
  response size limits, GPU runtime compatibility, or real-model performance.
- GGUF discovery reads bounded metadata; it does not validate complete tensors.
  Model loading remains an explicit user action.

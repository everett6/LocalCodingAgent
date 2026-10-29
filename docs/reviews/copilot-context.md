# Copilot context completion review

This review covers the live BM25 index, source snapshots and checkpoint fingerprint helper. It does not claim to review every source file or implement neural semantic embeddings, a learned reranker, a sub-200 ms retrieval guarantee, or isolation for arbitrary host commands.

## Corrected boundaries

- The index previously used `Path.read_bytes()` after checking metadata separately. A growing or replaced source could exceed its cap, and directly reading a FIFO could block. `context/source_snapshot.py` now opens regular files without blocking, rejects unsafe path components, reads at most the selected byte limit plus one overflow byte, and checks descriptor/path identity and metadata around the read. A frozen snapshot returns the exact bytes, SHA-256, size and modification time from that read. POSIX uses no-follow directory descriptors. The portable fallback validates resolved paths and descriptors; it is not an OS sandbox or a claim of race-proof Windows filesystem isolation.
- Automatic index discovery, live events and cache loading exclude common credential/state paths such as `.env*`, `.ssh`, `.aws`, `.codex`, `.local-coder`, and credential filenames. This is a path exclusion policy, not a detector for every secret embedded in ordinary source files.
- Cache loading is bounded to 64 MiB, refuses nonordinary/symlink files through the snapshot reader, validates persisted chunk shapes, and discards malformed entries. Regular source reads are capped at 512 KiB.
- Explicit refresh compares bounded raw-byte source hashes and reuses parsed chunks when those bytes are unchanged. On-demand and event paths also track inode/device/ctime alongside size/mtime. Same-size/mtime rewrites therefore invalidate the tested index, while metadata-only touches retain content generation. Content-derived generation is sorted by path, so discovery order does not change identity.
- Search hits carry the hash of their indexed source. Snippet loading drops hits whose current source differs, instead of mixing old symbol locations and new text. `file_hashes(paths)` provides an immutable dictionary of indexed source hashes for tool provenance; consumers should retain these hashes, rather than hash newer bytes after a response was returned.
- The watcher previously signaled readiness before initialization, and refreshed before the native subscription existed. It now waits for the first native batch/timeout, refreshes after subscription, and only then signals readiness. Concurrent starters wait on that same readiness event. Subscription failures remain visible, and stop joins the owned worker.
- Checkpoint fingerprint reads share the bounded source reader: at most 4096 files, 16 MiB per source and 64 MiB total. Exceeding bounds fails explicitly instead of silently omitting required evidence. Missing files remain absent so validation detects deletions; directory inputs are ignored as before.

## Read-time provenance integration

The original agent captured fingerprints only when saving a checkpoint. An edit after tool output but before checkpoint creation could bless stale conversation text with the new file hash. `ReadFileTool` now returns hashes from the exact bounded source snapshot; ranked search retains indexed source hashes, and known writes/edits report expected written-byte hashes. `BaseAgent` retains those observed hashes in checkpoint notes instead of blessing newer disk bytes, and restores them during resume. A rejected read no longer adds an unsafe path to checkpoint context or prevents the next recovery turn. Tests cover edits between reads and checkpoint creation. This protection covers these tool paths; it does not establish provenance for every initial prompt or repository-map fragment.

A file's current hash cannot prove that old conversation text came from that version. Likewise, index generation alone cannot establish that a returned snippet is fresh; consumers must keep its own `source_hash`. Repository-map consumers need to capture their index snapshot and hashes under the same lock, or reject a map if generation changes during assembly.

## Verification

`PYTHONPATH=src:/tmp/local-coder-merge-agent-deps python -m pytest -q tests/test_live_index_boundaries.py tests/test_source_snapshot.py tests/test_code_index.py tests/test_recovery_context.py tests/test_repo_map.py`

**69 passed.** This includes real native watchfiles add/edit/delete and shutdown, deterministic subscription/readiness failure/concurrent-start controls, same-size/mtime edits, source replacement/growth/FIFO controls, private-path exclusion, malformed cache inputs, stable generation and stale-snippet rejection. Compilation and `git diff --check` pass. No model calls, network probes, dependency installation, source downloads or commits were performed by this review.

## Remaining limits

The native watcher debounces events; a running model invocation does not receive instantaneous changed text. The caller must validate relevant source provenance before the next action. Filesystems and privileged external writers can have behavior beyond these local fixtures. Old cache files may still contain previously indexed material until the next successful cache write; filtering prevents those private entries from reentering active retrieval, but is not secure erasure of historical local storage.

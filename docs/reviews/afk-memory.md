# Local context efficiency and source-boundary review

Reviewed 2026-10-29 UTC against initial revision `2c74f0f`. Work is confined to this repository and disposable local fixtures. No model inference, paid API requests, model downloads or external scans ran.

## Changes and evidence

- Context memory now uses bounded regular-file snapshots with no-follow descriptor reads. Previously, a repository FIFO named `pipe.py` blocked indexing while waiting for a writer. A disposable baseline subprocess reproduced this and was stopped after one second. The fixed version returns a skipped-file result without waiting. Symlink aliases cannot verify stored source evidence.
- Common credential/state directories and private names/extensions are excluded from indexing and explicit source-bound ingestion. Baseline code indexed a synthetic `.aws/config` fixture. This was recorded as confirmed local ledger finding `F1` (`context/unsafe-source-read`). No real credentials were used.
- Memory list/structure requests and ingestion results share source hashes within that request, while the next request reads a new snapshot. Each saved record still compares its own source hash. Indexing uses its original snapshot and one revision lookup across files, rather than rereading each file during preparation.
- An unchanged index preserves static call edges instead of deleting and rebuilding them. Failed file preparation retains the previous records, which remain excluded from automatic retrieval if the source has changed. A replaced external symlink no longer aborts unrelated indexing.
- Candidate collection has a global cap across roots and bounds retained path objects even when one directory contains more files than the cap. The additive `truncated` flag reports an incomplete scan/file-limit result. Unvisited existing sources are retained. `os.walk` still enumerates each visited directory; this is not a guarantee of constant memory for arbitrarily large individual directories.
- Python feature hashing streams subwords without changing persisted vector values. Finite vectors supplied by a local embedding backend also normalize correctly when squaring would overflow or underflow; ordinary lexical normalization keeps its existing semantics.

## Controlled measurements

One synthetic Python file contained 100 symbols and 100,778 bytes. The comparison used the same local database, a warm filesystem, seven calls per operation, and a fixed revision lookup to isolate the changed work. Counts are per request; times are local medians, not performance guarantees.

| Operation | Before | After |
| --- | ---: | ---: |
| Memory list source reads | 100 | 1 |
| Structure source reads | 100 | 1 |
| Source bytes read by either inspector | 10,077,800 | 100,778 |
| Unchanged index SQL statements | 105 | 4 |
| Memory list median | 9.293 ms | 1.796 ms |
| Structure median | 12.652 ms | 5.421 ms |
| Unchanged index median | 12.050 ms | 3.799 ms |
| Long-token feature hashing peak traced allocation | 11,610,472 bytes | 195,505 bytes |

The hashing fixture used a single 192,000-character token and verified that both implementations returned exactly the same vector. Measurements and the disposable comparison script are retained in the ignored project scratchpad under `.local-coder/tmp/memory-efficiency-*` and `.local-coder/tmp/memory_efficiency_benchmark.py`.

## Validation and limits

The focused memory/context group passed **93 tests**, with **2 optional Rust-library tests skipped** because the native library was not enabled. Python compilation and changed-file whitespace checks passed. Regressions cover per-request freshness, private sources, FIFO handling, symlink changes, partial indexing, failed preparation, unchanged static graphs, hash compatibility and extreme local-vector magnitudes. Full integration checks are reported by the parent AFK delivery.

Context vectors and structured memory were already generated/stored locally by default; this change does not introduce a cloud embedding service or claim neural semantic retrieval. The default remains lexical subword hashing, while an explicitly supplied local backend is optional. Candidate/result limits, secret redaction and project database ownership checks remain in place. Concurrent external file edits may invalidate freshly indexed records; retrieval checks their source hashes before use. POSIX descriptor confinement is stronger than the portable fallback, and neither protects against arbitrary processes running as the same OS user. No existing user database or old memory history was deleted.

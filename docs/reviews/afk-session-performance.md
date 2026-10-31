# Bounded session journal snapshot cache

Implemented and checked 2026-10-29 UTC. Baseline comparison uses committed revision `1ec44b8`, which already contains the session corruption/path/omission safeguards. This change preserves those safeguards and the public session APIs; it does not migrate or delete history.

## Decision and behavior

Status polling previously reread, hashed, decoded, parsed and schema-validated the entire session journal on every `get` or `list`. A bounded per-`SessionStore` validated snapshot is worthwhile for repeated reads of an unchanged local journal. It trades retained local RAM for fewer filesystem reads and JSON parses; it does not save tokens by itself or change provider context.

- On supported platforms, only journals at most **8 MiB serialized** are cached. Larger journals keep the existing bounded uncached path. Parsed Python objects may occupy more memory than their serialized JSON; the limit is not a process RSS cap.
- Every cache attempt remains inside the advisory journal lock. Owned regular-file and no-link checks run before reuse. Descriptor-based POSIX metadata checks compare device, inode, size, modification nanoseconds and change nanoseconds; a second identity check catches changes during validation/copying. An unstable journal is retried once and then refused rather than returning known stale data.
- Atomic replacements, raw edits, same-size edits with restored modification time, deletion, unsafe replacements and read failures invalidate the snapshot. All writes clear the cache before replacement, including failure paths. Corruption is still preserved/refused rather than interpreted as empty history.
- Public mutable results are defensive copies. `get` copies only its selected record; `list` copies the returned collection. A caller cannot mutate the next status response by modifying a previous result. Internal borrowed data stays under the journal lock.
- Windows keeps journal caching disabled: Python documents different change-time semantics there, which do not establish the restored-mtime invalidation contract used here. This is a conservative fallback; Windows persistence continues through the uncached validation path. See the [Python stat timestamp documentation](https://docs.python.org/3.14/library/os.html#os.stat_result.st_ctime_ns).

## Offline measurements

A disposable fixture contained **250 sessions** and a **2,928,282-byte journal** with synthetic reports/events. Each operation ran fifteen times on a warm local filesystem. Warm median excludes the first call; read/byte totals include all fifteen calls.

| Measurement | Baseline | Cached |
| --- | ---: | ---: |
| Warm single-session `get` median | 5.648 ms | 0.172 ms |
| Warm full `list` median | 5.473 ms | 2.610 ms |
| Journal snapshot reads/parses per 15 calls | 15 | 1 |
| Journal bytes read per 15 calls | 43,924,230 | 2,928,282 |
| Estimated retained cache Python object graph | 0 bytes | 3,733,810 bytes |

The retained graph estimate sums unique JSON object sizes with `sys.getsizeof`; it is not a process RSS or allocator measurement. The savings justify the bounded cache for this fixture, especially active-job lookups that need one record. Frequent writes invalidate the cache, so busy workloads may see smaller gains. Results are synthetic/local measurements rather than a latency guarantee.

The comparison script and measurements are retained in the ignored project scratchpad at `.local-coder/tmp/session_cache_benchmark.py` and `.local-coder/tmp/session-cache-measurements.json`. No model inference, provider requests, user history or real credentials were involved.

## Verification and limits

The focused session, persistence, CLI, workbench and interface group passed **144 tests**. The new cache group contributes **20 passing tests**, including defensive copies, selected-record copying, external atomic replacement, restored-mtime in-place edits, corrupt/unsafe replacement, revoked permissions, parent-directory symlinks, deletion, failed writes, cache limits, racing updates and concurrent stores. Compilation and changed-file whitespace checks passed; the parent delivery records the full suite.

Metadata identity is a filesystem freshness mechanism, not cryptographic authenticity. It depends on the filesystem accurately reporting inode/change metadata and does not isolate arbitrary processes with the same OS privileges. Native Windows/macOS runs and unusual/network filesystem timestamp behavior were not verified. No SQLite migration, background service, auto-eviction, new model call or public API change was introduced.

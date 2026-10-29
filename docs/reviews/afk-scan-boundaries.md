# Local investigation source boundaries

Reviewed 2026-10-28. Scope: shared local scan enumeration/read helpers, Python bug heuristics, same-file semantic observations and Python sink candidates. Parent integration owns the SAST/config consumers in `tools/security.py`. No project code, blocking FIFO reproduction, external host scan, model request or system change was executed.

## Confirmed baseline gap

At baseline `2c74f0f`, enumeration rejected symlinks and large stat sizes but admitted non-regular directory entries such as a FIFO named `pipe.py`. Bug and SAST consumers then used `Path.read_text` after that preflight. A local untrusted repository could block a scan on a special file or race a source replacement/growth past the stat check. The findings ledger records this as **F6 / LOCAL-SCAN-UNBOUNDED-READ**, with the historical sink at `tools/bugs.py:37`. The implemented fix is verified by safe rejection/boundary regressions rather than intentionally blocking a scan.

## Implemented

- `scan_files` streams directory entries and accepts only regular files. Symlinks, unreadable entries/directories, special files, byte limits and traversal limits produce explicit partial coverage. Known dependency/build/state directories are pruned before descent. POSIX directory descriptors refuse link substitution at every opened component. Portable fallback validation is not an OS isolation guarantee.
- Enumeration is capped at 1,000 admitted files, 20 MB of preflight source sizes, 20,000 considered entries, 1,000 directories and depth 50. Limits and actual considered counters are included in coverage. Budgeted scans disclose that a narrower target is required; they do not claim to review the entire project.
- `read_scan_source(root, path, *, coverage=None, errors='strict')` returns UTF-8 text from a bounded no-follow regular-file snapshot, with a fixed 1 MB file allowance. It deliberately allows credential fixtures because security scanners need to inspect them. It does not reuse automatic-retrieval private-path exclusions.
- Passing coverage enforces a 20 MB read allowance across successful and failed attempts. `bytes_read` records successful snapshot bytes; `read_bytes_reserved` conservatively charges the allowed bound for failed/interrupted reads, and `read_budget_used` is the authoritative sum. Repeated reads are charged again. This prevents repeated failed/raced reads from escaping the aggregate bound. Errors propagate to the consumer, which records partial coverage.
- `record_scan_skip` retains at most 100 diagnostic strings, bounded per string, while preserving total and omitted counts. Existing public `skipped` and `status` fields remain available.
- Bug heuristics use bounded snapshots, 256 KB source units, 40,000 AST nodes and 500 findings per scan. Unsupported formats and complexity limits are explicit partial coverage. Async-body inspection stops at nested definitions, avoiding duplicate blocking-call observations and repeated nested-body walks.
- Same-file semantic analysis incrementally charges a 1 MB serialized-observation allowance before retaining repeated guard/route/service evidence. A final serialized-size check includes wrappers. The existing 256 KB source and 40,000-node limits remain. Over-budget units raise `AnalysisLimit`, which SAST reports as partial coverage.
- Python sink candidates now apply 256 KB source, 40,000 AST-node, 1,000-finding and 1 MB serialized-finding limits. This prevents repeated sinks on a long shared source line from duplicating that line into unbounded output. Limit errors remain explicit rather than returning an apparently clean empty scan.

The shared snapshot helper bounds reads and verifies inode/stat identity. This does not isolate the application from arbitrary same-user host processes, hostile filesystem implementations or OS-level I/O stalls. These changes do not establish exploitability, authorization effectiveness, whole-program dataflow or complete language coverage. Parent integration separately caps whole-scan result aggregation; this report's semantic limits are per source unit.

## Validation

`PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_scan_scope_boundaries.py tests/test_investigations.py tests/test_exploit_detection.py tests/test_secret_scan_boundaries.py tests/test_security_50.py tests/test_advanced_audit.py`

**86 passed**. Coverage includes FIFO/direct-target rejection, descriptor snapshot reads, credential-fixture access, growth after enumeration, conservative failed-read accounting, symlink replacement, traversal/depth/directory caps, bounded skip diagnostics, unreadable directories, nested async bodies, AST/semantic/finding budgets, explicit partial SAST coverage and existing positive/negative security detection cases. Changed Python modules compile, and whitespace checks pass. Parent integration runs the full suite separately.

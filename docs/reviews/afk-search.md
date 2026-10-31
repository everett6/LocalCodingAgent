# Read-only search boundaries and resource limits

Verified 2026-10-29 UTC with local generated fixtures and synthetic process lifecycles. This addresses the private-search fallback issue tracked as F9; no external targets or paid models were contacted.

## Behavior

Content search prefers ripgrep and uses its bounded NUL-delimited file enumeration, preserving its default ignore selection and explicit user glob behavior. Host dependency/generated-directory exclusions follow user glob arguments. `--no-config` and removal of `RIPGREP_CONFIG_PATH` prevent a user configuration from injecting preprocessors or alternate process behavior. Patterns remain arguments after `--`; no shell or Python regex evaluates the content expression.

Both backends apply the shared `private_read_path` policy, reject out-of-workspace targets, and exclude symlinks, special files and hardlinks before content search. Hardlink rejection is a stricter search-only policy: legitimate multiply-linked source files are skipped and reported through partial coverage. It is not an inventory of secret inodes or a DLP guarantee. Filename traversal prunes excluded/private directories before descent and uses descriptor-backed directory opens where supported.

Each source is read through a bounded no-follow snapshot. Only those bytes reach the native regex engine through stdin, so a path changed after the snapshot cannot redirect the child into a different file. Filename and line provenance come from the host, not from parsing filename prefixes out of matching text. Returned paths support colons and newlines in structured metadata; display escapes ambiguous filenames. Matched files retain SHA-256 hashes of the bytes actually searched, even if the file changes later.

When ripgrep is unavailable, the tool uses native extended-regex grep over the same allowed snapshots. Its fallback filename walk does **not** implement `.gitignore`; `gitignore_respected: false` makes that limitation explicit. Regex and advanced glob dialects can differ between the native engines. Dependency and private-content exclusions do not depend on that difference.

## Bounds and partial results

- At most 512 eligible files, 1 MiB per snapshot and 20 MiB aggregate successful/reserved source reads.
- A ten-second search deadline; child processes receive the remaining allowance. Timeout, cancellation and output overflow terminate and reap the owned process tree. Snapshot reads run off the event loop; an uninterruptible filesystem read cannot be forcibly stopped by a Python thread cancellation.
- Captured match stdout is capped at 64,000 bytes, enumeration stdout at 256,000 bytes and diagnostics at 4,096 bytes. Pipe readers run concurrently with stdin feeding. Overflow ends the child rather than draining an unlimited producer indefinitely.
- Returned content has a 64,000-byte conservative allowance covering rendered observations and structured path/hash metadata; the small fixed coverage object is separate. This avoids large filenames or repeated metadata bypassing the result-count cap.
- Results clamp to 1–200, with a default of 50. Long source lines are clipped from the original snapshot. More results, file/read/deadline limits, unsupported encodings, aliases and unreadable sources produce explicit partial coverage.
- The Python filename walk processes at most 10,000 entries, 512 directories and depth 32. For ripgrep, entry accounting means returned file records; native traversal itself is bounded by its deadline and captured output, not a claim that every visited directory entry was counted. This distinction appears in `enumeration_scope`.

The tool never presents a partial empty result as evidence that an entire repository lacks a match. Failed reads conservatively reserve their allowance separately from successful `bytes_read`. No source corpus or temporary copies are written to disk. Searches are sequential rather than launching hundreds of children simultaneously.

## Latency tradeoff

Three-run local medians on tiny generated files compared ordinary bulk search with a snapshot/stdin prototype, returning the same match counts. This is a deliberate privacy/resource tradeoff, not a speed improvement:

| Engine / files | Bulk search | Snapshot/stdin prototype |
| --- | ---: | ---: |
| rg / 100 | 0.0042 s | 0.1054 s |
| rg / 500 | 0.0051 s | 0.5274 s |
| grep / 100 | 0.0024 s | 0.1874 s |
| grep / 500 | 0.0041 s | 0.9404 s |

The prototype is roughly 25–230 times slower than bulk execution on these small fixtures, while absolute times remain under one second. The integrated tool then searched every file for an absent expression: 100/500 files took median 0.1559/0.7568 seconds with rg and 0.1387/0.6774 seconds with grep. These fixture timings do not predict latency on a user's filesystem or large sources. Broad searches should use a narrower target/glob when coverage limits apply; the existing indexed code search remains available for larger repository discovery.

## Verification

**60 focused tests passed**, including **27 new search regressions**, with compilation and whitespace checks. Tests cover both real local native engines, private/engine paths, symlinks/hardlinks/FIFOs, filename and source colons/newlines, forged carriage-return prefixes, ignore behavior, dependency pruning, enumeration/result/read caps, changed-path provenance, unsupported encodings, and synthetic overflow/timeout/cancellation process cleanup. No denial-of-service workload was run.

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_search_boundaries.py \
  tests/test_tool_efficiency_regressions.py tests/test_scan_scope_boundaries.py
```

Windows/macOS native execution, unusual filesystem stalls and arbitrary secret copies under ordinary public filenames remain outside the verified claims.

# Harness engineering roadmap

This roadmap groups the supplied engineering backlog into independently testable
changes. It is not a claim that every requested capability exists.

| Area | Existing foundation | Next work and acceptance evidence |
| --- | --- | --- |
| Context | Head/tail output truncation with spill files, old-output pruning, summaries, working memory, optional segment budgets | Invalidate observations after edits; retain failure summaries; test stale code never re-enters active context |
| Retrieval | Incremental BM25 code index; Python AST slicing; optional language parsers and LSP adapters | Content-based freshness and scope-aware hybrid reranking; measure retrieval quality on labeled queries |
| Token accounting | Conservative serialized-byte reservations and provider-reported usage | Host-selected tokenizer adapters; distinguish exact counts from estimates in API/UI; include tool schemas and generation reserve |
| Memory | Workspace-local persistent memory, goals, checkpoint recovery | Dependency-aware invalidation, bounded eviction, and retrieval audit records; test branch changes and restart recovery |
| Tools | Schema validation, MCP integration, scoped tools, patch/test/lint tools | Explicit capability metadata for parallel read-only execution and retry eligibility; verify mutations are never blindly retried |
| Execution | Shared budgets, checkpoints, bounded delegation, stagnation handling | Structured handoff evidence and per-task budgets; exercise cancellation, exhausted budgets, and failed workers |
| Security detection | Local scanners, findings ledger, security lessons, scoped local reproduction | Independent review records and revision-bound regression evidence; distinguish reproduction from proof of a fix |
| Persistence | Locked finding updates and atomic JSON replacement | Extend transaction boundaries to review sessions and append-only events before adding durable finding lifecycle states |
| Observability | Agent events, metrics, optional context snapshots | Display measured allocation and provenance; redact before persistence; expose uncertainty and missing provider metrics |
| Local inference | Provider profiles, role routing, optional native lexical hashing | Benchmark configured servers and hardware before changing quantization, batching, cache reuse, or GPU settings |

## Findings persistence

`FindingsLedger.record()` reloads the latest JSON snapshot while holding the
repository's cross-platform advisory file lock. Independent reviewers merge into
that snapshot and allocate IDs under the same lock. Saves use a private temporary
file, flush it, and atomically replace the ledger; an interrupted replacement
preserves the previous file. The existing list-of-objects JSON format is retained.

Unreadable or invalid ledgers produce an error instead of being interpreted as
empty. Preserve the original file before restoring a valid copy. Project state
paths reject symlinks using the shared state-directory guard. This does not isolate
the ledger from arbitrary host processes running as the same user, and advisory
locks only coordinate cooperating writers. Atomic replacement is not a guarantee
against every filesystem or machine power-loss scenario.

`save()` explicitly replaces a caller's snapshot and `reset()` explicitly clears
it; concurrent discovery must use `record()`. Reviews now retain the ledger across runs. The separate local security evidence
workflow adds persistent lifecycle events, independent review contexts, and
isolated before/after regression checks; see [local-security-evidence.md](local-security-evidence.md).

## Design constraints

- Record concise decisions, evidence, and unresolved questions; do not require
  private chain-of-thought transcripts.
- Keep core instructions stable and treat retrieved text and tool output as data.
  Delimiters supplement code-level permission checks; they do not enforce them.
- Use configurable context allocations. Fixed percentages and a 1,000-token
  output cap need evaluation against real tasks and tokenizer support.
- Preserve comments/docstrings when they convey contracts, security assumptions,
  or licenses. Do not strip them unconditionally.
- Do not use model agreement as proof of exploitability or fix correctness.
- Keep worktrees and rollback explicit. Failed tests must not erase unrelated
  user edits or trigger destructive automatic resets.
- Report exact tokens only when a compatible tokenizer/provider supplies them;
  attention distributions and GPU utilization require actual backend telemetry.
- Generated-code isolation, outbound network controls, and credential stripping
  must be enforced by the runtime, not only by prompt instructions.

## Reliability workflows

Evaluation reports, retrieval audits, model usage reservations, canonical findings UI, supported offline advisory ranges, explicit local tuning, Doctor and guarded read scheduling are now connected. Current behavior and limits are documented in [reliability-workflows.md](reliability-workflows.md).

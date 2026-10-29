# Research Upgrades Implementation Plan

> **For agentic workers:** Use subagent-driven development for independent modules.

**Goal:** Strengthen context retention, source analysis, safe recovery and evidence while accurately tracking all 200 requested upgrades.

**Architecture:** Extend bounded SQLite context and the existing tool registry. Add standalone analysis/reporting modules and authenticated workbench routes with a React capability inspector.

**Tech Stack:** Python AST, SQLite, Pydantic, existing local tree-sitter adapters, React, existing OS sandbox; no added dependencies.

**Spec:** docs/superpowers/specs/2026-10-25-research-upgrades-design.md

## Global Constraints

Preserve unrelated files; reject escaped paths and redact secrets; never execute inspected source; no invented verification, authorization or sandbox claims; no automatic paid fallback. External tools remain explicitly unavailable until configured.

## Review Focus

Changed/deleted helper files must not be retrieved as fresh; compaction must preserve tool call/result pairing; imported SARIF must not elevate verification; evidence tampering must be detectable; local recovery must not coerce safety refusals.

### Task 1: AST context and graph retrieval
- [x] Implement memory/code_structure.py and extend ContextMemory.structure(paths=None,char_budget=4000,limit=100), indexing and dependency expansion.
- [x] Verify nested symbols, cross-file helpers, bounded retrieval, stale/deleted paths and unsupported-language fallback in tests/test_code_structure_memory.py.

### Task 2: Context depth and recovery
- [x] Extend agents/base.py, context/compression.py and agents/frame_guard.py for dynamic output ceilings, successful-result pruning and visible noncoercive refusal handling.
- [x] Test native tool protocol preservation, quoted false positives, strict bounded attempts, original-message retention and ledger pinning.

### Task 3: Structural analysis
- [x] Add security_analysis.py and tools/advanced_audit.py; register read-only advanced_audit with security/reviewer/bug-hunter roles.
- [x] Verify weak crypto, dynamic SQL, unsafe deserialization, shell/URL/path sinks and redacted entropy candidates without source execution.

### Task 4: Evidence and interchange
- [x] Add security_reports.py: EvidenceStore, export_sarif, import_sarif, findings_report and reproducibility_summary.
- [x] Verify hash-chain tampering, bounded SARIF, CWE mapping, safe paths, secret filtering and unverified imported records.

### Task 5: Capability inspector and integration
- [x] Add research_capabilities.py covering all 200 exact titles and current implementation evidence; implement ResearchCapabilities.tsx.
- [x] Wire read-only capability/structure/report endpoints and explicit audit/import endpoints; extend both native bridge allowlists.
- [x] Test role boundaries, route validation, exports and missing external dependencies; build/lint UI and run full regressions.

### Task 6: Review and delivery
- [x] Independently review integration and update truthful capability statuses.
- [x] Publish a new integration PR based on reviewed main, then update installed applications after verification.

## GitHub integration

PRs 17–20 were already merged. Reviewed and merged PR 21 at `351de56`, then reapplied the unfinished research changes on `codex/research-integration`. Kept the goals screen, working-memory refresh, canonical security evidence lifecycle and plan restrictions. Added three missed blocked-result checks in new upstream coordinator paths, portable unavailable reporting for the POSIX regression backend, explicit partial coverage, and offline dependency inventory/advisory tooling. The original unfinished work remains in a Git stash backup.

## Delivery verification

Published PR 22 at commit `9f1d1e4`. Python: 1,220 passed, three optional skips (two native-memory checks and Go). Node desktop/IDE: 48 passed. Frontend build/lint and packaged PyInstaller/Electron builds passed. Packaged-engine smoke verified authenticated goals, model teams, local audit/report/journal routes, offline advisory import/matching and structure context. The installed native IDE extension-host smoke passed. Updated both installed engines and the manager resources with a rollback copy, preserved the root-owned sandbox helpers, reopened both native applications and confirmed the manager reached window-ready with working research APIs. At delivery the offline adversarial/native-memory CI workflow passed; desktop packaging and full IDE CI builds were still running.

# Evolving Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add local, evidence-led security reproduction tools, external context memory, adaptive model-aware scaffolding, language context and MCP connections without bypassing scope or approval controls.

**Architecture:** SQLite memory and declarative harness revisions supply bounded context and middleware. Every generated tool, delegated task, local reproduction, MCP call and file edit goes through the shared registry, safety scope, snapshot and request-budget boundaries.

**Tech Stack:** Existing Python/Pydantic/httpx/SQLite, stdio JSON-RPC, optional local tree-sitter/LSP executables, React and native Code OSS diff views.

**Spec:** User request in this chat, October 25, 2026; original implementation informed by official Harness², MCP and LSP documentation.

## Global Constraints

- Preserve unrelated local changes; implement in a managed feature worktree.
- No external attack targets, credential harvesting, host privilege changes, or generated-helper execution outside an explicit sandbox.
- Lower-tier exploit generation requires an injected human approval callback; no model-supplied confirmed=true bypass.
- New MCP servers and credentials must be explicitly configured by the user; no credential reuse from Codex/gh.
- Local embeddings do not download weights; report the embedding implementation accurately.
- Subagents inherit scope, shared budgets and the immutable approval boundary.

## Review Focus

- Prompt injection in retrieved memories, skills, tool descriptions and server responses remains untrusted.
- Changed files/HEAD and deleted memory rows cannot reappear as fresh retrieval hits.
- Cancellation reaps language servers, sandbox jobs and MCP children.
- Dynamic tool activation, profile overlays and delegation cannot grant additional capabilities.
- Snapshots and inspector writes preserve user edits and never expose secret values.

### Task 1: Scoped security generation
- [x] Add tools/exploit.py with GenerateExploitTool, privilege-path analysis and bounded fuzz case generation.
- [x] Test tier 4/5 automatic generation, tiers 1/2/3 approval/denial, file scope, deterministic local PoCs and report structure.
- [x] Extend existing RED_TEAM binding/agent instructions without duplicating routing.

### Task 2: External context memory
- [x] Add memory/context_store.py ContextMemory(project_root) with ingest, search, bounded retrieval, freshness, task state and graph links.
- [x] Add tools/memory.py wrappers; test isolation, redaction, indexing chunks, update/delete and concurrent access.
- [x] Integrate per-step retrieval and indexing plus inspector API/UI.

### Task 3: Language-aware context
- [x] Add context/language_service.py LanguageService(project_root) with 20+ adapters, AST symbol extraction and supervised LSP references/definition/hover.
- [x] Add tools/language_context.py; verify real protocol framing against fake servers, timeouts and unsupported capabilities.
- [x] Load bounded symbol context before edits and expose availability in the UI.

### Task 4: Adaptive scaffolding and profiles
- [x] Add models/profiles.py declarative role/family/user layers and harness/evolution.py versioned proposals and sandbox-gated generated tools.
- [x] Add evolution tools with injected delegation/sandbox callbacks, scoped inheritance and bounded fan-out.
- [x] Persist measured outcomes and expose revision history; never rewrite immutable safety checks.

### Task 5: MCP and skills
- [x] Add integrations/mcp.py with explicit private configuration, stdio/streamable HTTP transports and GitHub preset.
- [x] Add bounded discovery/call and on-demand builtin skill tools; test approvals, credential redaction, pagination and cleanup.
- [x] Add connection API/UI with no automatic remote authorization or installation.

### Task 6: Shared controls and integration
- [x] Add per-request token/cost budget ledger, pre-edit file snapshots, conditional rollback, sandbox adapter and editor diff views.
- [x] Wire new tools, automatic retrieval, model overlays, delegation and registry controls.
- [x] Run complete clean-worktree Python tests, frontend build/lint, desktop/IDE checks and synthetic end-to-end runs; document limits and ship reviewable source.

### Task 7: Automatic provider and local setup (user follow-up)
- [x] Add full live OpenRouter catalog, exact free variants, key-free saved profiles and native catalog selection.
- [x] Add daily request allowance admission, recoverable quota pauses and explicit paid continuation with spending limits.
- [x] Allocate mode-aware context within detected capacity and conservative request limits.
- [x] Reproduce the encoded-archive decompression failure and preserve raw archive bytes for checksum validation.
- [x] Discover local model/runtime metadata, expose searchable results, and save protected path-only preferences.
- [x] Prefill source-only engagement defaults, normalize URL schemes and simplify review setup.
- [x] Connect authenticated desktop and IDE routes and verify automatic setup without paid inference calls.

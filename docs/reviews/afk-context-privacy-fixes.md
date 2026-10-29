# Context privacy fixes: focused host boundaries

This change closes the reviewed generic file-read and model-visible tool-text paths (F7/F8). It is a focused plaintext control, not a complete data-loss prevention system. Verification used temporary local fixtures, synthetic credentials, and stub models; no real credential inventory or provider calls were used.

## Implemented contract

- `ToolRegistry` rejects generic `read_file` requests for common credential files and private engine state before execution, for every agent role. The shared `private_read_path(path, project_root)` checks both lexical and resolved workspace-relative names. Workspace ancestors such as `.codex/worktrees` do not make ordinary source files private. Unsafe/outside resolved targets are rejected. There is no approval override. Use the existing redacted security scanner or the dedicated role-owned `recover_context` operation.
- A new `ModelTextView` filters copied system/user/tool text at generation. Exact configured provider keys and common recognizable credential formats are redacted. The view follows the agent's current model dynamically, so host-side model replacement and checkpoint identity remain correct. Assistant content, tool arguments/IDs, reasoning and signature fields are unchanged to preserve provider protocol compatibility.
- Host-assembled file context omits private paths, and normal source text is filtered. Tool observations are filtered before fitting; changed observations carry a credential-redaction notice explaining that exact-match edits can fail. Model-based context summarization and draft-edit input use the same text boundary.
- Canonical tool results, original source bytes, source hashes and tool-owned local artifacts are unchanged. Truncated model-visible output retains a discoverable role-owned recovery reference even when a private spill file was written. Generic reads cannot bypass archive ownership; the local artifact remains available to the human owner.
- Coordinator initialization binds only explicitly configured provider values once: at most 64 unique strings of 8–4096 characters, longest first. It does not inventory ambient environment variables. Diagnostic events are copied and sanitized for handlers; original events, outer task/source identity and numeric usage/approval metadata are preserved. Generation error messages and emitted tool diagnostics use the same bounded secret binding.
- PEM filtering uses a single line/marker scan, avoiding repeated tail searches for unmatched headers. Complete blocks are masked; a standalone private-key header followed by a base64-shaped material line is masked through end-of-text if its closing marker is missing. Newline counts are retained. Documentation-only header examples remain readable.

## Scope and limits

This protects the agent gateway's generic file reads and the listed host-assembled model-visible text paths. It does not change direct host/editor utilities or claim to mediate every filesystem operation. Normal code inspection/editing and the existing local security scanner remain available. Search uses the exported path predicate in a separate change.

Exact key replacement deliberately ignores values shorter than eight characters and stops after 64 configured values. Recognizable formats are filtered independently. Arbitrary unquoted assignments, encoded/obfuscated secrets, unknown credential syntax, credentials introduced after binding, arbitrary hardlink aliases, and opaque assistant/provider protocol content are outside this filter's contract. Quoted prefixed fields such as `DB_PASSWORD` are filtered, while expressions such as `password = config.password` remain legible for analysis. These controls do not infer instructions from credential values or snippets.

## Verification

`PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_context_privacy_boundary.py tests/test_harness_recovery.py tests/test_token_efficiency.py tests/test_agents.py tests/test_context_gateway.py tests/test_context_provenance.py tests/test_copilot_compression_merge.py tests/test_merged_agent_context.py`

Result: **164 passed in 2.00 seconds**. Includes role/path protection, symlink handling, code-expression false positives, common and exact synthetic credentials, original source-hash preservation, full agent model egress, protocol object preservation, model replacement, Coordinator event copies and error sanitation, and own-role recovery versus foreign-role denial. A 1,500 unmatched-header fixture has a generous three-second ceiling to catch a quadratic regression, not to promise a latency target.

Owned-file `git diff --check` passed. Ruff was unavailable in the project virtual environment (`No module named ruff`); no packages were installed. Parent integration/full-suite verification is separate and remains pending at this report's creation.

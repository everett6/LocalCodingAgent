# Harness quality: evidence before feature count

Primary documentation checked 2026-10-29 UTC. These are design comparisons, not benchmark rankings or claims that Clin implements every product feature. No third-party repository instructions were adopted as authority.

| Reference | Documented mechanism | Clin decision in this improvement block |
| --- | --- | --- |
| [Anthropic: long-running harnesses](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents) | Incremental work, persistent progress and actual end-to-end verification help prevent premature completion and lost state. | Retain bounded child observations/checks in plan handoffs; preserve damaged journals; enforce task deadlines without replaying uncertain mutations. Existing checkpoints and verification remain host controls. |
| [OpenAI: Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/) | The loop constructs context, invokes tools and manages growing conversation state, including compaction. | Optimize local routing/context allocations while preserving complete source digests. Provider usage and request admission remain separate from the model's narrative. No provider-specific opaque compaction format is invented for unrelated models. |
| [Cursor: agent overview](https://cursor.com/docs/agent/overview) | Checkpoints let a user inspect and restore agent changes; browser tools support application checks. | Preserve the existing guarded Undo/diff workflow. Improve startup and task activity rather than add a second conflicting checkpoint mechanism. Undo still refuses later external edits. |
| [Claude Code: checkpointing](https://code.claude.com/docs/en/checkpointing) | Rewind can restore conversation and tracked code changes; external effects are outside a file checkpoint. | Keep unknown command effects explicit. A deadline cancels cooperatively, preserves the pending tool journal and requires recovery. A file checkpoint is not a promise to undo shell, database or network effects. |
| [GitHub: agent sessions](https://docs.github.com/en/copilot/how-tos/github-copilot-app/agent-sessions) | Sessions provide task progress and continuation across supported development surfaces. | Harden Clin's local session persistence and keep native surfaces on the authenticated workspace engine. This is not a new cloud synchronization service. |
| [Antigravity: browser overview](https://www.antigravity.google/docs/browser) | A separate browser subagent captures evidence; isolation and access policies are explicit parts of browser integration. | Retain explicitly approved local browser/DOM checks and distinguish captured evidence from verified assertions and vision review. Use disposable loopback fixtures to check the product UI. |
| [Aider: repository map](https://aider.chat/docs/repomap.html) | Relevant graph-ranked portions of the repository map fit an active context allowance. | Reduce redundant local source reads and unchanged graph rebuilds; preserve per-request freshness. Existing local hashing remains lexical retrieval, with no unmeasured neural-quality claim. |

## Why these changes came first

The code review found correctness gaps beneath the existing feature surface: routing diagnostics disagreed with runtime eligibility, post-readiness stdout could stop a healthy engine, a cancelled project picker could leave an invisible process, ordinary task deadlines were ignored, and plan handoffs discarded useful child results. Local memory repeatedly hashed the same source and rebuilt unchanged graphs. These are reproducible repository findings, not assumptions derived from another product's marketing.

Provider and session work is validated separately before being called delivered. A complete-looking tool argument from a length-truncated response is not permission to execute it. Missing usage is not zero usage. Invalid saved history is not empty history. Local scan limits must remain explicit rather than imply complete coverage.

## What this comparison does not establish

There is no claim of equal model quality, product parity, a universal on-device classifier, commercial release readiness or a complete codebase audit. Synthetic regression tests establish their tested contracts. Local microbenchmarks establish their fixture measurements. Live provider/model calibration, supported-platform packaging, signed distribution, real browser assertions and OS-level containment require their own evidence.

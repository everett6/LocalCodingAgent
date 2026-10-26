# Security review: where the self-improvement and compaction ideas come from

Research notes behind the lessons file, triage, findings ledger and batched
reviews (September 2026). Ideas were adapted to a small local model and this
codebase; no code was copied.

## What other agents do

| Agent | Compaction | Memory across sessions | What we took |
|---|---|---|---|
| Claude Code | Auto-compacts near the context limit; `/compact <instructions>` steers the summary. | `CLAUDE.md` files in the repo, versioned and shared. | A committed markdown file (`SECURITY_LESSONS.md`) as the durable memory, loaded into every review. |
| Codex CLI | Token-threshold auto-compaction; the summary is a *handoff* ("build on work already done and avoid duplicating it"), recent user messages kept verbatim. | `AGENTS.md`. | Batches hand off the ledger with "don't re-report these"; critical facts are pinned verbatim, not summarized. |
| opencode | Prunes old tool output first (protecting the newest ~40k tokens), then summarizes. | Rules files. | Already in draft PR #3; this change adds the ledger on top of it. |
| Cursor | Summarizes long chats. | Memories generated in the background **need your approval** before they are saved; team knowledge belongs in rules files. | The agent proposes lessons; a person accepts them. |
| Devin | Session handoffs. | Suggested **Knowledge** that you edit, accept or dismiss, with triggers for when it applies. | Same approve-first flow; lessons are grouped by when they apply (suppress, confirmed, pattern). |
| Google Antigravity | Planning, execution and verification artifacts (`task.md`, walkthroughs) carry state between steps. | **Knowledge Items** distilled from past conversations, loaded at session start. | Structured state kept outside the chat (the ledger) and distilled lessons loaded at start. |
| Anthropic security-review command | Separate false-positive filtering pass per finding. | n/a | Confidence 1-10 on every finding, report only 8+, plus a hard-exclusion list (DoS, rate limits, theoretical timing attacks, test-only code). |
| Anthropic long-running agent harness | Fresh context per session. | Progress file + feature list in the repo. | Each batch starts fresh and reads the ledger, like a progress file. |

## Decisions

- **No unsupervised self-modification.** "Self-improving" means the review
  learns reviewed lessons; it never edits its own prompts or code.
- **Deterministic over model-written where it matters.** The ledger is
  rendered by code, not summarized by the model, because a small quantized
  model's summary is exactly where a line number gets lost.
- **Fingerprints on line text, not line numbers**, so a baseline survives
  unrelated edits (the idea behind code-scanning partial fingerprints).
- **Scope unchanged.** Everything analyzes the local workspace. Nothing here
  needed network scanning, so no idea was dropped for scope reasons.

## Sources

- Claude Code memory: https://code.claude.com/docs/en/memory
- Compaction in Claude Code, Codex CLI, opencode, Amp: https://gist.github.com/badlogic/cd2ef65b0697c4dbe2d13fbecb0a0a5f
- Codex compaction analysis: https://kangwooklee.com/blogs/codex_context_compaction.html
- opencode compaction: https://opencode.ai/v2/docs/compaction/
- Cursor memories: https://localskills.sh/blog/cursor-memories-guide
- Devin knowledge: https://docs.devin.ai/product-guides/knowledge
- Antigravity context management: https://iceberglakehouse.com/posts/2026-03-context-google-antigravity/
- Anthropic, Effective context engineering for AI agents: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, Effective harnesses for long-running agents: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Anthropic security-review command: https://github.com/anthropics/claude-code-security-review/blob/main/.claude/commands/security-review.md

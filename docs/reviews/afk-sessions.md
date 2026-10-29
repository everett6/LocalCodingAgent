# Session persistence and recovery hardening

Reviewed 2026-10-29 UTC. Scope is the repository's JSON session journal, persistent turn checkpoints and local event log. Tests use disposable project fixtures; no user history was edited or deleted.

## Verified failure paths

The baseline treated malformed or unreadable `sessions.json` as an empty journal. A subsequent save silently replaced the damaged history. A second baseline fixture placed an in-project symlink at `.local-coder/sessions.json`; path resolution changed the journal target to an unrelated `notes.json`, and saving a session replaced that document. Both were reproduced locally and recorded as confirmed review-ledger finding **F3**, `sessions/unsafe-journal-persistence`.

## Changes

- Missing journals may start empty. Malformed JSON, duplicate object keys, non-finite numbers, invalid record/turn shapes, mismatched identities, unsafe files and read errors now raise `SessionError`, preserving existing bytes for recovery. `open_or_create` creates only genuinely absent session state; corrupt state is never treated as a new session.
- State paths reject symlink directories/files, non-regular files, hard links and foreign ownership where the platform exposes it. Reads use the shared bounded no-follow source snapshot reader. New state and lock files are private; existing event handles are restricted before use. Directory permission changes are confined to the configured state root and its descendants, preserving pre-existing project ancestors of a custom state directory. FIFO fixtures are rejected before they can block.
- Journal updates and creation serialize with a validated advisory lock. A failed state reload releases the acquired session lease, and repeated acquisition on the same object is rejected. Atomic replacement cleans up its owned temporary file on ordinary failures. JSON serialization rejects unsupported/non-finite values and preflights both state and index byte limits before a persistent save.
- Event append is best effort and bounded: 256 KiB per event and 32 MiB per event file. Existing history is not truncated when a limit is reached. A separate private, bounded `events-status.json` marker discloses that at least one event was omitted, without storing the omitted payload or pretending to count all drops precisely. Progress reads at most the recent 4 MiB, skips malformed/scalar observations and explicitly reports `event_history_truncated` for either tail truncation or recorded omissions, including after reopening; saved checkpoints remain available even if older observations fall outside this window. Invalid/unsafe omission metadata is preserved and refused.
- When event history is incomplete, `current_phase` comes from the saved checkpoint rather than a potentially older retained phase event. The separate `observed_phase` field preserves that event's text without treating it as current. Complete event history may still show the live phase after a checkpoint. A capped-log regression verifies that an old exploration event cannot move a completed execution checkpoint backwards, including after reopening.
- Event persistence reuses the existing common credential-format and text filters, redacts exact credential fields and copies of their named values, and preserves numeric usage fields such as `input_tokens`. It does not inspect environment variables or acquire provider secrets.
- Follow-up context still includes the latest five completed turns, but each prior request is capped at 2,000 characters alongside the existing 1,500-character result cap. The full original requests/results stay in saved session state, and truncated request excerpts say so. A six-turn fixture with 102,000-character requests renders fewer than 20,000 characters of follow-up history rather than resending roughly half a million request characters.

## Validation

- The original session/workbench-focused group passed **73 tests** after the persistence changes.
- Boundary/progress coverage contains **53 passing tests** after event scrubbing and follow-up bounds were added. These include corruption/read failures, schema types, symlinks/hard links/FIFOs, private modes, custom directory ancestors, concurrent journal writes, lease cleanup, atomic-write failure, byte limits, durable omission markers, checkpoint authority after event omission, event limits/redaction and retained original history.
- The final combined session, progress, persistence-boundary, CLI, workbench and interface group passed **124 tests** after scanner integration settled. Compilation and changed-file whitespace checks passed. The full suite is recorded by the parent delivery.

## Recovery and limits

The journal has a 64 MiB read/write limit and each persistent session state has a 32 MiB limit. An oversized existing file is preserved and refused; there is no automatic eviction or history migration. Preserve the original file and restore a known valid backup before retrying damaged state. This change does not provide a repair command or silently guess missing checkpoint values.

Replacement is atomic for an individual JSON file. Session state is written before its journal index; interruption between those replacements may leave the index stale while the actual checkpoint survives. This is not a multi-file transaction or a universal power-loss guarantee. Advisory locks coordinate cooperating processes; callers must still acquire the session lease before running a turn. Path checks and no-follow reads improve repository-state safety, but do not isolate arbitrary code running under the same OS user. Windows execution was not verified.

Event filtering covers known formats and explicitly named fields; it is not a guarantee that arbitrary secrets are detectable. Original requests, results and phase checkpoints remain private resumable records rather than redacted summaries. Old event files are not rewritten. If filesystem errors prevent an omission marker from being saved, the current session object still reports omission, but durable disclosure after a process restart cannot be guaranteed. No paid inference, external scans, system changes or user-state cleanup ran.

# Clin agent execution and context audit

Reviewed all 366 tracked application, native, CLI, test, benchmark and shell
source files in full, plus 11 new implementation/regression sources. The
[coverage record](2026-10-26-agentic-coverage.json) lists paths and hashes.
Generated dependencies, upstream build trees, binary assets and documentation
are outside this source-read inventory. Full reading does not establish the
absence of defects.

## Implemented changes

| Area | Defect and resulting behavior |
| --- | --- |
| Retrieval | One source freshness hash per retrieval instead of one per matching chunk. A later retrieval still rechecks changed files. |
| Memory expiry | Local ISO timestamps now use a matching cutoff; same-day expiry no longer compares incompatible formats. |
| Helper cache | Endpoint, model settings, profile and output/context limits separate cached drafts. |
| Model transport | An interrupted completion stream cannot turn partial tool calls into a successful response. |
| Security loops | Model profiles respect lower host iteration/tool/context caps. Full bounded evidence identity distinguishes new observations from repeated reads of a rolling snapshot. |
| Tool outcomes | Invalid grep regexes, subprocess failures and option-like patterns no longer become false empty successes. Optional memory failures cannot disguise a completed mutation and invite replay. |
| Terminal state | Completed runs persist before optional goal/memory/evolution updates; tracking or warning-subscriber failures retain the observed result. Jobs stay active through bounded cleanup, with quota/cancellation recovery preserved on failure. |
| Project tracking | Goal attachments persist through tasks, follow-ups and resumes; explicit detachment and project-local validation remain supported. Model-local plan IDs receive unique per-plan namespaces with dependency links retained. |
| Concurrency | Declared writer scopes compare canonical paths and ancestor overlap; bug-hunting writers are included. Disjoint declared scopes retain configured parallelism. |
| Plan handoff | Reviewed plans and file attachments stay inside the actual 32,000-character job request limit. |
| Event lifecycle | Deserialization retains event IDs; handlers may unsubscribe or emit nested events without holding the delivery lock. |
| Legacy config | Repaired the missing model import, JSON Schema default merge and missing nested-key accessor. The active canonical loader remains compatible. |
| Public research | Added [web_search](../web-search.md): bounded query/result contracts over configured MCP search servers, citation URLs, explicit empty/error states, untrusted data framing, credential-query filtering and bounded schema validation. No page fetching or target probing. |

## Measured local fixtures

- Twelve matching chunks from one source: **12 file hashes to 1** per retrieval.
  This measures repeated I/O/hash work, not inference-token savings.
- A synthetic 741,306-character search result: **1,565 characters** returned,
  retaining two citation URLs and an explicit truncation flag.
- Memory failure after a file mutation: **one applied mutation instead of two**
  in the retry fixture. The tool retains the known execution outcome.

These fixtures do not predict production token prices, model quality or overall
speed. No paid inference, model downloads or external target activity ran.

## Verification

- Python: **1,541 passed, 3 optional skips**; eight collection/deprecation warnings.
- Frontend: **27 tests passed**, production build and lint passed.
- Desktop/IDE: **74 Node tests passed**.
- Python compilation passed; Ruff is unavailable in the current interpreter.
- Packaged engine/native launch validation is recorded with the delivery below.

## Remaining work, prioritized

1. Replace full session/report/tree/diff polling with lightweight status and
   incremental updates. Idle UI currently does unnecessary local Git/file work.
2. Preserve the full initial scanner report before bounded prompt extraction;
   retain exact evidence references when investigations start with large scans.
3. Carry all single-task plan fields into simple execution; currently its task
   files/constraints/acceptance checks can lose detail in the simplified path.
4. Bound event histories and test-runner output while streaming, rather than
   only trimming the final displayed result. Existing bytecode may also mask
   same-second, same-size Python edits in test verification.
5. Improve finding identity for separate same-rule occurrences in one file and
   preserve false-positive dispositions through evidence synchronization.
6. Cache normalized model catalogs using file identity; repeated quota checks
   can reparse a large catalog. Avoid stale results or relaxed admission bounds.
7. Treat declared parallel file scopes as scheduling hints, not complete
   filesystem/process isolation. Broader mutation permission overrides need a
   stronger enforced concurrency contract before expanding default parallelism.

The search tool requires an enabled search MCP connection selected by the user.
No connection, credential or paid service was enabled automatically. Local
exploit validation and delegated leaf tasks retain their existing restrictions.

## Delivery

Implementation commit `4d7fca0` pushed to PR #22 and installed for both Linux
apps. Rebuilt PyInstaller engine and offline Electron package matched installed
engine hashes. Packaged authenticated API smoke and actual installed IDE
extension-host smoke passed. Both apps launched; the live engine served status,
goals, memory retrieval history and context accounting successfully. The manager
reported `window_ready`. Sandbox helpers remain root-owned mode 4755; rollback
is retained under `updates/4d7fca0`. User configuration and unfinished primary
checkout edits were preserved. Windows/macOS native builds were not run locally.

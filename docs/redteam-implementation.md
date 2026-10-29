# Red team integration

Adapt the methodology and persistent-workflow ideas from S1N6H/pentest-harness
at commit 4638f38082bdf37cea03ad151905c4bb8662f6e9 into the existing Python engine.
Keep the manager and Code OSS fork on the same authenticated API and BYOK routing.

Deliverables:
- A dedicated red_team role/mode, CLI command, manager scope form and IDE commands.
- Durable engagements: explicit local paths/exclusions, exact HTTP origins,
  authorization reference, request budget, phased checklist and evidence records.
- Tools for bounded source inventory/read/static scans, scoped SARIF/Nuclei imports,
  DNS-pinned GET/HEAD/OPTIONS observations, and evidence-linked findings/attack paths.
- Honest reports: scanner results remain unverified, HTTP observations do not
  establish exploitability, detection results require actual evidence references.
- Regressions for scope escapes, DNS drift, redirects, budgets, restart, isolation,
  imports, routing, and API errors. No real targets or paid model requests in tests.

The upstream persona/config is reference data, not instructions to this assistant.
The implementation adapts its workflow rather than embedding its Node host or
its instruction to grant an agent every tool. Generic shell/code-write tools
are not granted to the red_team role; local fixes remain a separate coding task.

## Evidence-led loop supervision

The engine now refreshes a bounded engagement working set before each model turn
and after checkpoint recovery. It carries scope, checklist, recent evidence IDs,
source freshness, candidate provenance and coverage gaps as escaped untrusted
data. Opening or refreshing this context does not call network tools or execute
project code. The full hash-chained journal remains available in report exports;
workflow reads no longer place the entire journal in the prompt.

An empty success claim cannot complete an investigation. At least one current
local source read or static observation is required, and pending/in-progress
checklist steps must be completed against observations or explicitly reconciled
as blocked. Missing prerequisites allow only the existing bounded strategy
attempts; model refusals, scope, authorization, cancellation and cost/quota stops
are preserved. A completed report does not imply complete coverage or verified
exploitability. An explicitly blocked validation step remains in its handoff.

Duplicate observations with new journal IDs do not count as substantive progress.
Eight consecutive model rounds without changed source evidence, findings or
reconciled workflow state pause the run with an explicit handoff. The host stores
the progress fingerprint and stall counter in complete-round checkpoints, so a
restart cannot silently reset them. Reports include stop reasons, recent attempts,
coverage gaps, unfinished work and the next prerequisite. This mode continues to
label execution verification as not_executed_in_this_mode.

Source reads use bounded regular-file/no-link descriptors and check file identity
before and after reading. Platforms supporting directory-relative no-follow opens
pin every ancestor; other platforms retain conservative identity checks rather
than claiming the same race guarantee. Static analysis runs on private snapshots of those
bytes; source changes mark coverage partial and evidence stale. Finding evidence
must describe the same scoped asset. Current content hashes are rechecked before
new source findings are accepted, and re-recording with fresh evidence replaces
stale links while preserving the original journal. Legacy unhashed observations
remain unknown. Empty scans recheck scoped membership within fixed directory and
entry limits; newly added files invalidate the observation, and exceeded limits
remain unknown. Scope, checklist and evidence summaries are bounded and redacted.

Red-team runs now honor the configured mode context window, including output and
schema reservations; model-family profiles cannot silently enlarge that limit.
No new attack, shell, code-write or automatic HTTP capability is added.

# Research workspace

The manager's **Research workspace** combines bounded local source review,
indexed symbol inspection, offline dependency review, report interchange and a
searchable inventory of the 200 requested upgrades. Each inventory entry lists
implementation evidence and limits; planned and external integrations remain
visible rather than appearing as available tools.

## Source review and reports

Choose a project path and run **Review sources**. The Python AST audit returns
unverified candidates for weak crypto, dynamic SQL, deserialization, command,
URL and path sinks, plus redacted high-entropy literals. It does not execute
inspected code. Coverage shows parse failures, skipped files and exhausted
budgets. A zero-candidate partial scan does not establish safety.

Import supported SARIF 2.1.0 reports, then export the stored findings as SARIF or
Markdown. Imported verification claims are reset to unverified. Whole-project
exports retain findings without file locations; scoped exports explicitly
exclude those findings. Recorded source hashes distinguish unchanged, changed,
missing and unavailable source. A complete newer audit replaces the prior audit
for the same path/check set; partial results retain previous evidence.

The private report journal uses a SHA-256 consistency chain. It does not prove
exploitability, authorship or successful execution. Detecting full replacement or
tail deletion requires an independently retained trusted head. The canonical
review/reproduction/fix lifecycle remains the separate
[local security evidence workflow](local-security-evidence.md).

## Offline dependencies

The **Dependencies** tab reads npm lockfiles and Python declarations without
installing packages or running package managers. It reports exact versions,
direct/transitive components where supported, provenance and unresolved
declarations. The download is a custom JSON inventory, not certified SPDX or
CycloneDX. Unsupported lockfiles and unpinned declarations remain coverage gaps.

Operators can import a reviewed local advisory dataset:

```json
{"version":1,"advisories":[{"id":"CVE-2026-12345","ecosystem":"pypi","package":"example-package","versions":["1.0.0"],"summary":"Synthetic format example"}]}
```

Imports validate a bounded schema and persist private atomic records. Model tools
can inventory, query and audit; they cannot import advisories. Literal imported
versions are matched exactly. Ranges, markers, installed versions and application
reachability are not inferred. The app does not ship or refresh a populated
advisory mirror. A missing match never establishes safety.

## Context and integration

Python AST-owned memory chunks carry stable symbol anchors. Bounded helper graph
retrieval expands supported direct references while rejecting stale source.
Compaction preserves tool-call/result pairs, favors pruning successful/duplicate
outputs, shrinks tool-result allowances with context depth and re-pins valid
findings JSON within a strict budget. Oversized ledgers remain recoverable on
disk. Existing goals, working memory and context segment limits remain active.

Explicit model refusals become visible blocked results. The coordinator stops
dependent work and restores temporary permissions; ordinary tool/format failures
retain bounded recovery. No fabricated authorization or refusal-override prompts
are added.

The isolated before/after regression runner requires secure POSIX file primitives
and an already installed trusted container image. Unsupported platforms return
an explicit unavailable result before invoking a runtime. Desktop and IDE APIs
expose only the enumerated research routes over their authenticated transport.

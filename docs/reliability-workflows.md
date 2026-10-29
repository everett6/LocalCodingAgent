# Reliability workflows

The manager now connects evaluation, memory provenance, canonical findings,
request accounting, dependency evidence, local inference tuning and setup
repair. Tools can run independent declared reads concurrently, while mutations
retain ordered barriers and durable recovery markers.

## Evaluations

Open **Harness evaluations**. The offline mode parses nine fixed synthetic
Python cases: vulnerable, fixed and harmless examples for dynamic evaluation,
shell use and deserialization. Reports retain a suite version/hash, false
positives/negatives, precision/recall and observed elapsed time. These small
fixture scores are not real-world coverage or proof of security.

**Run configured model evaluation** requires explicit confirmation. It uses the
configured security model with the shared token budget, dollar cap and provider
quota guard. Cloud, metered and unknown-cost models require a positive operator
spending cap before a backend is constructed. Providers never change through an
automatic paid fallback. Reports distinguish actual provider usage from missing
metrics; cancellation releases the workspace job slot and closes the backend.

Project remediation counts are separate: successful fixes require fresh canonical
assertion-failing before evidence and passing after evidence for the same tests,
image and backend. Stale or incompatible evidence cannot improve these counts.
The production improvement hook evaluates the selected security model against
this same fixed suite with the candidate instruction profile rendered into its
measured prompt. It uses a dedicated backend with shared budget/quota controls.
Paired profile evaluation retains separate validation/activation stages; a passing synthetic benchmark is not a guarantee for arbitrary tasks.

## Memory

**Project memory** shows recent retrieval decisions, selected/excluded candidates,
scores, reasons, provenance and context budgets. Audit storage is bounded. Source
hashes determine content freshness across branch changes: unchanged code remains
usable; changed/deleted source is excluded. Long-lived decisions and task state
are retained separately. Human corrections remain searchable as
`corrected_unverified`; they are not presented as verified source text or used
to reconstruct an original code graph.

## Findings and verification

**Findings & verification** exposes the canonical candidate/review/reproduce/fix
workflow. Review and regression preparation use the configured model and existing
approval/budget controls. Long operations have jobs, events and cancellation.
Verification requires an explicit local test, before/after phase and an already
installed trusted Docker/Podman image. It never pulls an image. Source revision
changes invalidate accepted evidence. See [local-security-evidence.md](local-security-evidence.md).

## Request accounting

**Harness controls** shows message/tool-schema/framing reservations, output
allowances and provider-reported actual usage separately. A compatible local
Transformers tokenizer can measure exact chat-template components. Complete
provider request formatting remains unverified, so overall accounting and
admission retain conservative bounds.

Tokenizer configuration is operator-controlled and workspace-local. Loading is
offline with remote code disabled. Asset validation rejects links, external
references, model weights and oversized bundles; measurement inputs are bounded.
Missing libraries, incompatible templates, changed/deleted assets and excessive
inputs retain conservative accounting rather than blocking application startup.

## Dependency evidence

The **Research workspace → Dependencies** view supports reviewed v2/OSV records
in addition to the original exact-version format. npm comparator sets/caret/tilde
and OSV intervals have bounded semantics. PyPI range evaluation uses the optional
installed packaging adapter. Unsupported grammar remains unresolved; prereleases,
withdrawn records and inclusive/exclusive interval endpoints are distinguished.

**Refresh advisory** is an explicit request for one public CVE/GHSA/PYSEC record
from the fixed official OSV origin. Redirects, environment proxies, credentials,
compressed responses and unbounded transfers are rejected. No automatic mirror
or comprehensive coverage is claimed. Missing matches never establish safety.

## Local inference tuning

Local-model settings include a tuning panel. Explicit trials benchmark the
configured runtime and model with bounded variants and fixed benign prompts.
Reports use actual streaming time to first content and provider/runtime-reported
output tokens; throughput includes prefill/request overhead. Missing metrics are
not guessed. Memory/VRAM telemetry is currently unavailable.

The baseline configuration and prior running state are restored on success,
error or cancellation. Shutdown and runtime starts share a lifecycle gate.
Applying a result requires an in-memory trusted measurement from this engine
session, compatible files/settings, at least 5% greater measured throughput and
TTFT within 10% of baseline. Historical records are for inspection, not activation.
Speculative trials require an already configured compatible draft and runtime
support. No model/runtime is downloaded during tuning.

## Setup Doctor

**Setup doctor** inspects selected runtime/model files, provider/key presence,
private engine discovery metadata, installed IDE sandbox permissions and project
state. It does not execute found programs, contact providers or search the
computer on page load. Engine port information is metadata, not a live connection
test. Repairs are offered only when supported and recheck/verify their target:
reset an errored app-owned runtime, remove a verified stale descriptor, or make
owned project state private. Publication and stale removal share a private lock.
Administrator ownership/permission changes remain outside automatic repair.

## Tool scheduling and recovery

Only concrete known reads or explicitly declared safe read tools overlap, at
most four at once. Results preserve model-call order and IDs. Mutations remain
barriers; permissions, schemas, scope, cancellation and context still apply.

Pending journals record per-call effects and completion. A mutation whose
completion is unknown—including a partial patch or quota error inside its batch—
retains the last complete checkpoint and replay guard. Recovery identifies the
uncertain operation and requires inspection rather than repeating it. Ordinary
completed test failures remain available for debugging.

## IDE update sandbox continuity

Linux fork updates preserve the already configured root-owned Chromium sandbox
helper only when its bounded SHA-256 matches the incoming regular helper.
Changed or unconfigured helpers stop publication with a manual setup message.
Failed transfer/publication restores the original directory and helper; if that
rollback also fails, both recovery paths are retained rather than deleted.
A completed update moves the configured helper forward: its historical directory
backup contains the incoming unconfigured helper. Manual rollback must move the
configured matching helper back, or perform administrator sandbox setup, before
launching that backup. The updater never disables the sandbox or changes ownership.

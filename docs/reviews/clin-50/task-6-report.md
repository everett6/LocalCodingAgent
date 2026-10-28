# Task 6 — benchmark and release evidence

Scope: items 15, 48, 49 and 50; local implementation and focused verification. No model/provider requests, downloads, package installs, new credentials, user-environment changes, commits or publication were performed. Tests use explicit local test doubles; they are not real-model quality evidence.

## 15 — native release matrix

- Existing independent `ubuntu-latest`, `windows-latest`, `macos-latest` desktop test/build matrix retained. Added focused release/evaluation/profile regressions and native IDE protocol tests on every platform.
- Unsigned desktop packaging retains `--publish never`; ordinary builds need no signing credentials.
- Automatic IDE upstream discovery, PR preparation, Linux build/smoke and artifact upload remain available. Ordinary native job now has `contents: read`; automatic unsigned publication removed.
- Publication is a separate explicit `workflow_dispatch` input (`publish: false` default), default-branch-only, `release-approved` environment job. It requires an independent evidence run containing both the exact native archive/manifest and release-approval evidence. It checks current revision, archive digest, manifest-bound evidence digest, trusted public-key fingerprint, detached GPG signature, clean source and all readiness attestations before any `gh release` action.
- `desktop/verify-release.py` uses an ephemeral private GPG home with no key retrieval, never generates/imports private keys, and never edits the user's GPG configuration. The public verification key/fingerprint are external repository environment variables, currently unprovided.
- Native workflow YAML parsed and critical gate policy inspected locally. GitHub Actions was not dispatched. All native runner statuses remain `unverified`; Linux local Node tests are not native build evidence. Windows/macOS runtime and Code OSS packaging/smoke remain unverified.
- The release publication signature gate does not establish client-side update signature validation, macOS notarization, Windows signing or a commercial signing policy. Those remain external/missing readiness evidence.

## 48 — held-out benchmark gates

- Frozen `local-workflow-holdout-v1`, SHA-256 `e489679cf38ca9b40e0cdb35718c3076d0cf4331e76876247b61caf434386c8b`, contains six local coding, bug and security tasks: normalize-name implementation/control, clamp repair/control, unsafe YAML-loader finding/safe-loader control.
- Security holdout uses a vulnerability class/source distinct from adaptation's eval/shell/pickle fixtures. No held-out source/answer is returned by the catalog. The SHA covers version, prompts, source and expected outcomes.
- Host-only adapter accepts no adaptation overlay, forbids inspected-code execution, checks bounded JSON and AST-equivalent patches, and calls the supplied backend through the same BudgetLedger/quota guard. Explicit spending admission, timeouts, cancellation and known/unknown usage/cost are preserved. No provider fallback is introduced.
- Both tagged runners and tagged result reports are rejected by `improvement_cycle`; profile comparison also rejects held-out reports.
- Added authenticated host route `POST /evaluations/heldout/run`, requiring confirmation and exact displayed suite hash; uses the existing workspace job slot, cancellation/cleanup, selected configured security-role backend and admitted shared budgets. GET `/evaluations` includes catalog and a separate bounded private held-out history. History validation reconstructs aggregate quality from fixed case outcomes and rejects invalid resources/suite identities.
- Static detector results, local transport test doubles and actual model jobs retain separate modes/claims. The adapter explicitly reports unverified model identity/quality; a host-selected model identity is provenance, not proof of real model quality.
- Limits: this is a small public synthetic release regression holdout, isolated from application adaptation. External pretraining contamination cannot be excluded. Patch acceptance is AST equivalence, not broad functional testing or complete coding workflows; controls report unnecessary changes as false positives. No real model was run and no general security/coding quality claim is made.

## 49 — model-profile regression gate

- Profile-rendering model adapter labels reports `profile_regression` and includes actual model-call/token/configured-price cost/latency, completion and false-positive observations. Tool failures are zero because this adapter exposes no tools; invalid response/backend failures abort validation.
- `compare_profile_reports` requires same fixed-suite/model profile reports, complete finite observations, nondecreasing completion, nonincreasing false positives/cost/tokens/calls/tool failures/response errors and latency within the explicit default 1.25 ratio. It requires an actual quality/resource improvement; latency noise alone cannot qualify. Unknown cost or other required metrics fails closed.
- `improvement_cycle` invokes this gate for actual profile reports and persists its result; validated revisions still require existing separate activation approval.
- Added `async validate_profile_revision(store, revision: dict, evaluator, *, timeout_seconds: int = 30) -> dict` to close the direct propose/validate path. It compares active baseline and candidate through the same admitted async evaluator, records observed baseline/candidate/gate evidence and returns numeric measurements with success. Parent is wiring `tools/base.py` to this hook; this worker does not claim that cross-owned integration passed before parent verification.
- Existing generic benchmark callers preserve their prior measurements/behavior; the measured profile path is stricter. The comparison helper assumes host-owned reports, not arbitrary model-submitted metrics. This narrow fixed suite is not broad model selection evidence.

## 50 — commercial readiness gate

- New `readiness_report(project_root) -> dict` reads a bundled exact copy of `docs/roadmap/clin-50-spec.json`; tests compare all 50 IDs, titles and acceptance strings to the source spec. No blanket implemented flags or file-presence-as-pass logic.
- Every acceptance item has separate status, evidence and missing gates. File existence/hash is only available-source evidence. Default all 50 items remain unverified pending actual reviewed evidence. Report is wired as `/harness.readiness` (the `readiness` member of existing authenticated GET `/harness`), preserving the rest of that payload.
- Commercial fields: licenses, brand clearance, update signing, key storage, privacy, rollback, support. Separate native platform and model-benchmark evidence fields default unverified. Unknown/missing external inputs block `ready`; dirty/unknown source trees also block readiness.
- Actual root license paths and bundled notices are inspected and hashed. Root project LICENSE is currently absent, a release blocker. Actual installed direct Python distribution metadata and license texts were observed for pydantic, pydantic-settings, httpx, rich, click, PyYAML and jsonschema; no license for this project or complete npm/Electron/Code OSS/transitive inventory is invented. Complete reviewed bundled-license inventory remains unresolved.
- Optional operator-controlled `.local-coder/release/evidence.json` contains per-item/commercial/native/model attestations. Each requires exact current revision, reviewer, verified declaration and bounded in-workspace references with matching SHA-256. Stale, escaping, symlinked, changed or missing referenced files remain unverified. `attested` clearly identifies externally supplied review, not an independent legal conclusion generated by this program. The publication path additionally binds these attestations to a signature by the independently trusted public key and requires explicit release approval.
- No trademark, legal, privacy-policy, key-storage audit, support commitment, rollback review, signing/notarization, native runner or real-model claim is inferred. Default `ready` observed false, with all external commercial/native/model gates missing.

## Coordinated native bridge additions

- Parent-authorized narrow allowlist updates in desktop/IDE for GET `/status?summary=1`, existing GET `/jobs/:id`, GET/POST exact `/integrations/search/doctor`, GET exact `/context/debugger`, and POST `/evaluations/heldout/run`.
- Invalid summary values/extra operations, arbitrary doctor subpaths/query execution and mismatched methods are rejected. Existing authenticated transport retained; no general MCP call or model action endpoint added.

## Verification evidence

Regression-first failures observed for absent held-out catalog/runner, profile comparator, readiness report, publication verifier, direct spending admission, held-out API confirmation/history, direct profile validation hook, harness readiness field and search-doctor bridge paths. Implementations followed those failures.

Final focused command:

`PYTHONPATH=src:/tmp/local-coder-merge-agent-deps python -m pytest -q tests/test_release_50.py tests/test_harness_api.py tests/test_coordinator_evaluation.py tests/test_harness_evaluation.py tests/test_improvement_loop.py tests/test_improvement_recovery.py`

Result: **79 passed** (local Linux; Python 3.14 in this environment). No complete Python suite was run, per assignment.

- `node --test desktop/tests/*.test.cjs`: **29 passed**.
- `npm test --prefix ide`: **47 passed**.
- After final allowlist additions, `node --test desktop/tests/release-50-routes.test.cjs desktop/tests/reliability-routes.test.cjs ide/extension/test/harness-routes.test.cjs`: **9 passed**.
- `compileall` passed for new readiness/evaluation/improvement/API/publication helpers; final diff whitespace check passed.
- Workflow YAML parsed; matrix and publication-default/environment/permission policy checked. No runner execution inferred.
- Attempted `PYTHONPATH=src:/tmp/local-coder-merge-agent-deps python -m build --wheel --no-isolation --outdir /tmp/clin-release-wheel`: **blocked**, `Backend 'hatchling.build' is not available`. No dependencies installed. Wheel/native build success unverified.
- Signing negative regressions reject missing/public-private key confusion, digest mismatch and invalid public-key/signature material. No valid signing keys supplied/generated, so the positive signature/publication path is explicitly unverified.

Parent remaining work: wire and verify the direct validation hook in registry/coordinator; aggregate cross-worker tests; review package manifest inclusion; preserve missing external gates. No external evidence can be manufactured by local tests.

# Local security evidence workflow

This workflow reviews already identified findings in the selected repository.
It does not scan external systems. Model confidence and scanner labels are
claims, not reproduction evidence.

## Discovery and independent review

Run `local-coder --project /path/to/repo security` as usual. Reviews retain their
findings across runs and include a bounded Python AST threat inventory: entry
points, input boundaries, sensitive operations, auth-related observations, and
heuristic import/call links. CWE and OWASP references guide investigation.
These links are not proven interprocedural data flow. Dynamic dispatch,
non-Python code, runtime configuration, and dependency internals remain gaps.
ATLAS mappings require AI-specific evidence and are not inferred from generic
Python calls. Offline adversarial cases cover encoded instructions, malicious
repository notes, forged tool logs, and MCP metadata in retrieved evidence;
they test engine boundaries, not a live model's resistance to every attack.

```sh
local-coder --project /path/to/repo security-findings threat-model
local-coder --project /path/to/repo security-findings list
local-coder --project /path/to/repo security-findings prepare FINDING_ID
local-coder --project /path/to/repo security-findings review FINDING_ID
```

`prepare` uses the exploit-validator role to write a self-contained pytest test
under `tests/security_poc/`. It cannot execute tests or commands, change production
files, or confirm a finding. The regression asserts the **desired safe behavior**:
it must fail by assertion against vulnerable source and pass after a fix.
This differs from the legacy `validate-finding` demonstration PoC, which asserts
insecure behavior. A legacy PoC cannot be used unchanged as a safe-behavior
regression.

Inspect the generated test. Then `review` creates a fresh reviewer context with
read-only tools and asks it to challenge input control, reachability, prerequisites,
impact, guards, and counterevidence. Strict JSON validation rejects extra lifecycle
or evidence fields. The reviewer uses the configured reviewer model, which may
be the same model as discovery; a separate context is not statistical independence.
Neither command has external scanning tools. Live model commands need credentials.

Findings are keyed by a hash of their normalized path/rule-or-title and persisted
in the workspace's private SQLite store, with transactional lifecycle events.
Source changes make review/verification evidence stale. Changed finding details
invalidate prior review and regression evidence. Human suppression reasons remain
until explicitly reopened:

```sh
local-coder security-findings suppress FINDING_ID --reason 'Reviewed fixture-only behavior'
local-coder security-findings reopen FINDING_ID
```

## Before/after verification

Provide an already-installed trusted image containing Python, pytest, and the
project's dependencies. The runner never pulls images or installs packages.
For this development environment, `local-coder-regression:pytest` was built with
Python 3.12 and pytest 8.4.2 for a self-contained fixture; arbitrary projects may
need additional dependencies in their own reviewed image.

```sh
local-coder --project /path/to/repo security-findings verify FINDING_ID \
  --phase before --test tests/security_poc/test_access.py \
  --image local-coder-regression:pytest

# Apply and review the production fix, keeping the regression unchanged.

local-coder --project /path/to/repo security-findings verify FINDING_ID \
  --phase after --test tests/security_poc/test_access.py \
  --image local-coder-regression:pytest
```

Use `--backend podman` to select Podman and `--timeout 60` to set a bounded timeout.
The runner takes a filtered source snapshot, excluding symlinks, private state,
credentials by filename, dependencies, and common build caches. Limits are 2,000
files, 64 MiB, and 10,000 traversed entries; exceeding a limit fails rather than
silently executing a partial snapshot. Exclusions are returned with each result.
Secrets embedded in ordinary source are not guaranteed to be detected by these
filename exclusions; only use reviewed local tests and images.

Containers use a non-root user, no network, read-only source/root filesystem,
dropped capabilities, no-new-privileges, and memory/CPU/process/output limits.
Host environment variables are not forwarded. Pytest project configuration,
conftest files and plugin autoload are disabled; tests should use inline fixtures.
A trusted read-only plugin records test node IDs and assertion failure types.
Output and reports are bounded; timeouts/cancellation remove the named container.

Lifecycle gates:

- `candidate`: discovery or model claim only.
- `reviewed`: supported structured review against the current source revision.
- `reproduced`: the current reviewed source failed an executed regression by assertion.
- `fixed`: the same test content, test IDs, container image ID, and backend passed
  against a different source revision.
- `reverified`: a previously fixed finding passed another matching verification.
- `reopened`: an after-fix run failed again. `suppressed` requires a recorded human reason.

Collection/setup errors, ordinary runtime exceptions, zero-test runs, missing
images, timeouts, and changed workspaces never establish reproduction or a fix.
The source is fingerprinted before and after execution; changing it during a run
rejects the result. Fingerprints are sequential snapshots, not filesystem-wide
atomic transactions. Test-generated reports are evidence, not cryptographic
attestation; a malicious test or compromised container runtime can undermine them.
Human review must establish that the test actually covers the reported issue.

## Evaluation

`security-findings metrics labels.json` computes precision, recall, and explicit
true/false-positive and false-negative ID sets from:

```json
{"predicted_ids": ["case-a", "case-b"], "expected_ids": ["case-a", "case-c"]}
```

Undefined ratios are `null`. Duplicate IDs count once. Use stable labeled safe and
vulnerable cases; do not treat these metrics as general model accuracy. Threat
inventory coverage gaps are reported separately. Live-model accuracy and cost per
confirmed finding have not been benchmarked by this implementation.

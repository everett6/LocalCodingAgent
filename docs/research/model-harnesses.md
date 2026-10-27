# Automatic model and mode scaffolds

Selecting a model automatically resolves its harness at every agent step. No new
provider configuration or extra API key is required. Open **Harness → Automatic
model harness** to inspect the selected identity, task role, workflow, ranking
provenance, endpoint capability status, and context/tool ceilings. Changing task
mode changes the preview; Refresh rereads the selected provider.

The offline Intelligence Index snapshot contains 50 distinct model identities and
94 benchmark variants. See [collection notes](model-index-sources.md) for sources,
identity mapping, reasoning-variant grouping, and refresh limitations. Exact IDs,
canonical catalog slugs, AA names and recorded variant names match explicitly.
Unknown releases use labeled family or generic defaults, never a guessed rank.
Only 32 identities have exact OpenRouter mappings; 18 lack verified callable IDs.
The snapshot does not add inaccessible models to a provider's catalog.

Scaffolds compose model identity, curated family defaults, verified provider
capabilities, configured physical context, and the agent's role. Less documented
families use conservative defaults. These are engineering policies, not 50
individually benchmarked optimizations. AA scores describe model evaluations;
they do not establish this application's security-review effectiveness.

## Distinct specialist workflows

| Role | Investigation plan | Evidence requirements |
| --- | --- | --- |
| Cybersecurity | Entry points → trust boundaries → callers/guards → disproof → ledger | Read-only local review; reachability, preconditions, counterevidence, coverage gaps |
| Bug/bounty hunting | Invariant → falsifiable hypothesis → reproduction → minimal fix → regression | Local failing test and passing negative control; explicit suspected/reproduced distinction |
| Red team | Saved engagement → fresh scoped sources → reconcile hypotheses → local lab proposal → report | Existing immutable engagement; same-asset evidence, stale-source checks and unresolved prerequisites |
| Finding validation | Recorded finding → preconditions → regression/control → observed impact | Existing local finding only; no invented test execution or impact |

Specialist tool priorities differ, but selection intersects the registry's
existing permitted tools. Tool discovery cannot grant authority. Red-team memory
continues through bound engagement evidence rather than general project retrieval.
Existing agent lifecycle, scope, safety, approval, sandbox and ledger checks remain
authoritative. Profiles never raise configured retries, token limits or spending.

## Runtime behavior

* Family defaults choose bounded schema, retrieval and tool-result ceilings.
  Smaller configured context windows reduce them further. User limits still win.
* Tool support is a provider-specific catalog fact. OpenRouter snapshot facts are
  only applied to its official HTTPS API endpoint. Local/self-hosted endpoints
  with the same model name remain capability-unverified. Missing metadata is
  unknown, not proof of support. Explicitly unsupported tools are withheld.
* Switching models replaces the previous runtime guidance, refreshes schema and
  admission budgets, and emits `profile_selected` only when the effective profile
  changes. Model configuration/credentials are not serialized in this event.
* Profile reductions do not permanently shrink configured strategy or output
  ceilings after a switch. The original configured ceiling remains authoritative.
* Variant reasoning efforts and AA fallback labels are identification evidence.
  They never automatically change paid provider invocation settings or claim
  parity with AA benchmark runs. No inference or model download occurs on lookup.

The bundled snapshot is refreshed through a reviewed source update, not silently
rewritten from remote text. It is included in both Python distributions and the
desktop engine. Offline tests exercise all 50 identities across the specialist
roles, endpoint isolation, bounded overlays, model switches, context admission,
and registry intersection. Real-model quality and cost comparisons still require
paired task evaluations with explicit provider access and measured outcomes.

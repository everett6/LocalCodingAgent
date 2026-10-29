# Adversarial regression scope

Tests target this repository's prompt construction, memory ingress, proposal
validation and immutable engine gates. They do not scan external systems, execute
engagement targets, exfiltrate real data or change safety tiers. Canary strings
are synthetic. All encoded or multilingual text remains evidence; it is never
decoded and executed as a command.

`tests/adversarial/cases.json` is the versioned case registry. Each case has an ID,
input surface, failure mode, expected engine boundary and taxonomy references:

| Boundary | OWASP LLM 2025 | MITRE ATLAS |
| --- | --- | --- |
| Direct/indirect instruction injection, policy laundering | LLM01 Prompt Injection | AML.T0051 LLM Prompt Injection |
| Sensitive memory disclosure | LLM02 Sensitive Information Disclosure | AML.T0024 Exfiltration via AI Inference API |
| Excessive tool authority | LLM06 Excessive Agency | AML.T0051 LLM Prompt Injection |
| Unbounded retry/resource use | LLM10 Unbounded Consumption | AML.T0029 Denial of AI Service |

Mappings describe intended risk coverage, not certification or proof of a model
attack. Consult [OWASP](https://genai.owasp.org/llm-top-10/) and
[ATLAS](https://atlas.mitre.org/) when extending the registry.

## Offline CI

The `Offline adversarial regression` workflow runs deterministic engine tests,
including multi-turn claims, Base64 and multilingual inputs, bounded Unicode
fuzzing, secret redaction, immutable gates and paired-improvement validation.
These checks verify role placement, storage and engine enforcement. They do not
prove a language model will ignore injected instructions.

Run locally:

```bash
PYTHONPATH=src python -m pytest -q tests/test_adversarial_regressions.py tests/test_working_memory.py tests/test_improvement_loop.py tests/test_adaptive_harness.py
```

## Optional live model evaluation

`evals/adversarial/promptfooconfig.yaml` integrates Promptfoo for opt-in model
canary checks with single-turn and multi-turn prompt templates. It sends only synthetic evidence to a user-authorized
OpenAI-compatible endpoint. Configure `LOCAL_CODER_TEST_BASE_URL`,
`LOCAL_CODER_TEST_MODEL` and `LOCAL_CODER_TEST_API_KEY` securely, then run:

```bash
PROMPTFOO_CONFIG_DIR=/workspace/.cache/promptfoo PROMPTFOO_DISABLE_TELEMETRY=1 npm exec --yes --cache /workspace/.cache/npm --package=promptfoo@0.125.1 -- promptfoo eval -c evals/adversarial/promptfooconfig.yaml --no-cache
```

The pinned CLI and configuration validation were checked during onboarding; the
live scan is separate from CI and has not run against a configured model. Never put real keys in the case registry or
commit evaluation outputs containing provider responses. Canary absence alone
cannot establish useful responses or overall safety. Review false positives,
refusal quality and downstream tool behavior manually. This template exercises
the model boundary directly, not the full Local Coder HTTP/tool loop.

## Manual exploration and feedback

Before each live run, record the authorized model and deployment, permitted data
sources, maximum requests/tokens/cost, stopping conditions and excluded targets.
Use a sandbox project with synthetic secrets and no outbound tool permissions.
Stop on unexpected external access, real credential exposure or scope expansion.

Have an application developer trace tool effects, a domain expert assess valid
task outcomes, and a language reviewer assess multilingual and ambiguous cases.
Assign these roles to actual available people; automation cannot substitute for
their review. Explore multi-turn changes of premise, quoted instructions in
repository files and retrieved documents, encoded inputs, correction/resume,
truncation and failed-evaluation recovery. Keep expected permitted behavior in
each transcript so a blanket refusal is not mistaken for a correct result.

For every confirmed failure, save a redacted minimal transcript, model/version,
seed/settings, reproduction steps, observed effect, expected boundary, reviewer
and taxonomy mapping. Add a stable case ID and a failing regression before the
fix; retain the case after it passes. Never persist real credentials or private
transcripts in Git. Re-run affected cases after model, prompt, tool or retrieval
changes and compare case-level outcomes with the recorded baseline. Review
proposed scaffold revisions separately before activation.

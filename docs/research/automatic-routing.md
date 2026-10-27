# Cheap-first automatic routing

The default is a **hybrid router**, not a large model delegating every request.
Local rules handle unmistakable small tasks without inference. An optional local
classifier handles uncertain *read-only* requests. The selected model handles
protected roles, difficult work, unknown instructions and retries. Existing
bounded subagents remain useful for genuinely independent work inside a larger
task; routing does not create more agents or expand their permissions.

| Approach | Cost/latency tradeoff | Use here |
| --- | --- | --- |
| Local rules | No classifier call; limited coverage | First decision, with conservative risk gates |
| Small local classifier | Local computation and memory; classification can be wrong | Optional, short read-only candidates only |
| Large model delegating | Pays for the parent call, repeated context and child calls | Existing complex-task planning, not a prerequisite for simple requests |

## Setup

Open **Harness → Automatic routing**. Rules are enabled by default, but they need
an eligible configured economy model. With a paid OpenRouter model selected, the
host can derive `deepseek/deepseek-v4.1-flash-1017` from the **existing cached
catalog** when its records are fresh and it has full pricing, native tools, and a valid context window.
All three exposed input/output/request rates must be no greater than the selected
model's cached rates, and at least one must be lower. No network request is made
to decide this. Missing prices, a free primary, another endpoint or a local-only
session prevents that automatic synthesis. Actual total savings depend on usage
and have not been measured.

The model ID is verified on the [official OpenRouter model page](https://openrouter.ai/deepseek/deepseek-v4.1-flash-1017).
The `10127` spelling is not used as a guessed callable ID.

For an optional classifier, load a small GGUF through the application's existing
local-model setup, select your cloud provider, then enable **Use an already
loaded on-device classifier for cloud tasks**. This reuses the app-owned loaded
runtime; it never starts another process, evicts a model, or downloads weights.
[Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B) is a sub-1B option; its latency,
memory use and classification accuracy require testing on the actual device.
There is no universal-device performance guarantee. Configure its runtime/chat
template for non-thinking output for this short classification task; the official
model card documents `enable_thinking=False` and `/no_think` controls. No claim is
made that a configured classifier has passed a quality or performance benchmark.

Advanced users can name existing YAML model keys instead:

```yaml
routing:
  auto_enabled: true
  economy_model: economy
  local_classifier: tiny
  classifier_timeout_seconds: 2.0
  allow_cross_provider: false
```

These keys reference separately configured `models` entries. API keys remain in
environment references or existing provider storage, never routing preferences.
Preferences are private project state in `.local-coder/routing/settings.json`.
Saving requires an idle harness and does not infer, probe, download or start a
model. A configured local endpoint is shown as unverified until a matching
app-owned runtime is known ready; merely naming it does not establish readiness.

## Runtime contract

* An exact narrow read such as `Summarize README.md` can use a single read-only
  explorer instead of paying for planning, writing, review and test stages.
  Existing saved plans retain their execution path.
* Exploration, simple execution, DAG tasks and existing leaf delegation consult
  routing. The actual resolved model then selects its normal model/mode scaffold.
* Explicit task models, retries, cybersecurity, bug/bounty hunting, red-team,
  review and finding-validation roles retain the selected model. Security terms,
  complex work, extra mutation instructions, long prompts and uncertain language
  do not receive a classifier downgrade. These heuristics are conservative
  routing decisions, not a security boundary.
* Classifiers must be loopback OpenAI-compatible/llama.cpp endpoints. No arbitrary
  DNS endpoints, hosted Anthropic defaults, environment proxies or redirects are
  allowed on classifier transport. The host sends a bounded untrusted objective,
  with no tools, 128 output tokens and a 0.25–5 second timeout. Only exact JSON
  `{ "tier": "economy", "confidence": 0.95 }` (or `selected`) is accepted;
  economy requires at least 0.90 self-reported confidence. That threshold is not
  calibrated probability or proof of correctness. Malformed replies, errors and
  timeouts retain the selected model.
* All classifier/model calls use the existing shared budget and quota guard.
  Decisions are cached in a bounded per-manager cache using complete task digests
  and routing identities. Telemetry contains a reason, source, selected/economy
  slot and digest; it does not contain the prompt or classifier response.
* Economy models and their fallback chains must satisfy endpoint and tool
  capability checks. Local tasks never automatically move to cloud. Moving
  between cloud endpoints requires the explicit cross-provider preference.
  A free primary never receives a synthesized paid fallback. Manual configured
  economy choices remain visible and subject to the same execution gates.

Permissions, scopes, approvals, checkpoints, cost admission and verification are
unchanged. Failed mutations are not automatically replayed merely because a
cheaper model was chosen. Paid-model quality, local-classifier calibration and
cost/latency comparisons remain externally unverified.

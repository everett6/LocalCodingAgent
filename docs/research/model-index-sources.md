# Artificial Analysis Intelligence Index snapshot

Retrieved at 2026-10-27T05:27:26+00:00. The bundled JSON contains **50 distinct model identities**, selected by first appearance in the [official Artificial Analysis LLM leaderboard](https://artificialanalysis.ai/leaderboards/models?deprecation=all). Their 94 reasoning/fallback/non-reasoning variants are preserved as nested evidence. The 50th identity first appears at source row 90. All names, displayed scores, creator labels, and model-analysis links were directly verified in the public HTML. 32 identities map to exact public OpenRouter catalog identities; 18 remain explicitly unmatched.

## Ranking interpretation

`model_rank` is the one-based ordinal among distinct identities. `rank` is the best displayed leaderboard row for that identity. Variants retain their original row positions, names, scores, and source links. Grouping removes only terminal effort/fallback/non-reasoning suffixes; explicit releases, dates, sizes, Preview, Full, Open, and experimental labels remain separate identities. Thus this is a derivation from the official leaderboard, not an assertion that AA publishes a distinct-identity ranking. All source rows for the selected identities were retained, including variants below the 50th identity's first appearance.

Integer-score ties preserve source order and do not imply strict differences in underlying scores. `score_display` preserves source asterisks without inferring their meaning. The table displays Status:Current even though its URL says deprecation=all; we record the displayed table rather than claiming deprecated-model completeness. The index version v4.3.2 was verified on the [official index page](https://artificialanalysis.ai/evaluations/artificial-analysis-intelligence-index); its [methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking) explains the composite evaluation. This is a dated snapshot, not a guarantee that rankings or availability remain current.

## Identity and capabilities

Exact identities were independently checked against the [public OpenRouter catalog](https://openrouter.ai/api/v1/models), retrieved by an unauthenticated HTTPS GET. Exact release/date distinctions are preserved. Creator branding and hyphen/space typography differences are recorded in `openrouter_name`. `openrouter_id` and `openrouter_canonical_slug` come directly from the catalog; aliases contain the AA base name plus those exact catalog values. Canonical slugs are OpenRouter catalog identities, not independently verified native-provider IDs. No native-provider IDs were inferred. `family` is a local category derived from visible names, not an official catalog field.

Capabilities preserve catalog facts: supported parameter names, input/output modalities, context window, and maximum output tokens. They do not certify task quality or availability through each provider. `catalog_reasoning` records provider catalog metadata. Variant `aa_reasoning_effort` copies the source suffix; `reasoning_effort` is present only if that exact effort is explicitly supported in the catalog. **Variant effort fields are identification evidence only. They must never automatically change provider invocation settings, reasoning behavior, or spend.** Rows without effort suffixes do not acquire an inferred effort.

For Anthropic “with fallback” variants, base identity and effort are verified, but the AA fallback behavior is not reproduced. No exact benchmark-configuration parity is claimed. Unmatched identities have no fabricated capability metadata or callable IDs.

Unmatched identities: Muse Wonder Full, Ling 3.1 Flash 1019, Qwen3.8-Flash-Next, Mistral Large 4 Preview, Agnes 2.5 Pro Beta, DeepSeek V4 Flash Vision, JT-4.1 Flash 236B A21B, Motif 3, K2 Horizon 375B A23B, Beam 1.0, Nex-N2-Pro, Quasar 438B, Solar Pro 4, Apodex 1.1, GPT-5.5 Instant (June 2026), K2 Horizon MoVA 36B A4B, Hy3, Qwen3.7 Plus. Similar catalog names were deliberately not substituted (for example, Muse Wonder versus Full; Ling Flash versus 1019; Mistral Large 4 versus Preview; DeepSeek Vision versus Vision Exp). This records uncertainty rather than making unverified aliases.

## Collection and refresh

The first HTML tbody was parsed in source order. Both official sources were retrieved without credentials. SHA-256 hashes of the original HTTP bodies and the UTC retrieval timestamp are stored in the JSON. A refresh must recheck source order, grouping, exact identities, and supported capabilities. No API keys, paid calls, inference requests, or model downloads were used.

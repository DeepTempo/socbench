# socbench results

> **No published numbers yet.** This is the results-table skeleton. Every row
> is filled in from a `summary.json` (per run) and an `ablation_summary.json`
> (per `dataset_hash, seed`). The exact commands that produce each row live in
> [`RESULTS_REPRODUCE.md`](RESULTS_REPRODUCE.md).

## How to read this document

- Numbers are **never updated in place.** A change to any of
  `prompts_manifest_sha`, `playbooks_manifest_sha`, `tools_manifest_sha`, or
  `pricing_snapshot_date` produces a **new** dated section, never an edit to an
  existing one.
- Scoring is **three co-primary lenses** (per-flow / per-IP-pair / per-host
  F1), all macro-averaged. Cost is in USD at the stated pricing snapshot.
  Reliability is `first_pass_valid_rate` (fraction of renderings that returned
  a valid `submit_assessment` within budget, without force-final-answer).
- Strata are `(unit_type, gold_label)`: `{pair_timeline, host_egress} ×
  {benign, malicious, mixed}`.

## Provenance header (copy per published section)

| Field | Value |
|---|---|
| Dataset | `<dataset name>` |
| `dataset_hash` | `<hash>` |
| `sample_seed` | `<seed>` |
| Mode | `smoke` \| `full` |
| `prompts_manifest_sha` | `<sha>` |
| `playbooks_manifest_sha` | `<sha>` |
| `tools_manifest_sha` | `<sha>` |
| `pricing_snapshot_date` | `<YYYY-MM-DD>` |
| Models | openai=`<model>` anthropic=`<model>` gemini=`<model>` |

---

## Dataset: `<dataset name>` — `<YYYY-MM-DD>` snapshot

### Headline (main) — F1 / cost / reliability

One row per `(provider, persona)`, macro-averaged across all strata.

| Provider | Persona | per-flow F1 | per-pair F1 | per-host F1 | cost (USD) | first-pass valid |
|---|---|--:|--:|--:|--:|--:|
| openai | soc_analyst | – | – | – | – | – |
| openai | threat_analyst | – | – | – | – | – |
| openai | adversary_hunter | – | – | – | – | – |
| openai | detection_engineer | – | – | – | – | – |
| anthropic | soc_analyst | – | – | – | – | – |
| anthropic | threat_analyst | – | – | – | – | – |
| anthropic | adversary_hunter | – | – | – | – | – |
| anthropic | detection_engineer | – | – | – | – | – |
| gemini | soc_analyst | – | – | – | – | – |
| gemini | threat_analyst | – | – | – | – | – |
| gemini | adversary_hunter | – | – | – | – | – |
| gemini | detection_engineer | – | – | – | – | – |

### Per-stratum per-flow F1 (main)

One row per `(provider, persona)`; one column per stratum. Empty strata
(reported by the sampler as `stratum_undersampled`) are marked `n/a`.

| Provider | Persona | pair·benign | pair·malicious | pair·mixed | host·benign | host·malicious | host·mixed |
|---|---|--:|--:|--:|--:|--:|--:|
| openai | soc_analyst | – | – | – | – | – | – |
| … | … | | | | | | |

### Ablation deltas

From `ablations/<dataset_hash>/<seed>/ablation_summary.json`. Positive delta =
`main` scored higher, i.e. the ablated layer was contributing lift.
`single_shot_baseline` is the external single-shot run on the **same** eval
units (populated only when its full artifacts exist).

| Ablation | Provider | Persona | Δ per-flow F1 | Δ per-pair F1 | Δ per-host F1 | Δ first-pass valid | Δ cost (USD) |
|---|---|---|--:|--:|--:|--:|--:|
| tools_off → main | openai | soc_analyst | – | – | – | – | – |
| playbooks_off → main | openai | soc_analyst | – | – | – | – | – |
| single_shot_baseline → main | openai | soc_analyst | – | – | – | – | – |

### Cache (cost attribution)

From the `summary.json` `cache` block.

| Provider | cached tokens | hit rate | savings (USD) |
|---|--:|--:|--:|
| openai | – | – | – |
| anthropic | – | – | – |
| gemini | – | – | – |

---

_Add a new dated section above this line for each fresh manifest/pricing
combination. Do not edit historical sections._

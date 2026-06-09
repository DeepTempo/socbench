# ADR 0002 - Frontier LLMs as SOC Agents on NetFlow

- Status: draft
- Date: 2026-05-22
- Owners: Mayank Kumar
- Affected components: `src/tempoalpha/inference/llm-benchmarks/`*, `prompts/playbooks/*`, `prompts/shared/playbook_common_v1.md`, `prompts/compose/compose.py`, `build_index.py`, `tools/*`, `run_benchmark.py`, `pricing.yaml`, `benchmark_config.yaml`

## Brief description

This ADR specifies how we benchmark frontier reasoning LLMs as **SOC agents** on raw NetFlow data. Each model is given a deterministic, pre-indexed view of an investigation unit — either an IP-pair timeline or a host-egress slice — and a persona-scoped suite of read-only tools that retrieve flows, summaries, and rollups from a content-addressed corpus index. Four security personas (SOC Analyst, Threat Analyst, Adversary Hunter, Detection Engineer) each run a bounded multi-turn agent loop with a fixed dollar cap, persona-tuned turn/tool/wall-clock budgets, and a strict final-answer JSON contract. Per-flow, per-IP-pair, and per-host F1 are scored co-primary; cost is tracked across three rollup layers including cached input tokens; mandatory `tools_off` and `playbooks_off` ablations sit alongside a triple-off baseline so every published number is paired with attribution. The benchmark is distributed as a pip-installable open-source repository runnable locally from a Jupyter notebook or CLI script, so any team can reproduce the headline numbers and ablation deltas on their own API keys.

## 1. Context

This benchmark answers a different question than a single-shot raw-flow comparison: **what does it look like when a frontier LLM is wired up the way a real security team would actually use it — with persona prompts, role-specific tools, and a multi-turn investigation loop?**

The task is unchanged at the bottom: classify NetFlow records as benign or malicious and identify which specific flows, IP-pairs, and hosts are malicious. The change is the input shape:

- **Pre-indexed corpus, not inlined flows.** Stage A normalizes parquet datasets once into a content-addressed index of flows, pairs, hosts, and pre-computed rollups. Stage B reads from this index.
- **Logical investigation units, not arbitrary token chunks.** The unit of evaluation is an IP-pair timeline or a host-egress slice — choices that match how SOC analysts actually scope investigations.
- **Tools, not raw context dumps.** Each persona accesses an allowlisted set of read-only tools that query the index. Investigation scope is open via tools; scoring is closed to the seeded eval unit.
- **Multi-turn agent loops with bounded budgets.** Each persona has a turn count, tool-call count, wall-clock, and per-rendering dollar cap. The benchmark measures behavior under these bounds, not unbounded reasoning.
- **Reproducible from a developer laptop.** Public users `pip install`, set three API-key environment variables, and run a smoke under a $10 budget on their own keys.

This ADR locks the index format, eval-unit definitions, agent loop budget, persona × tool matrix, playbook structure, scoring lenses, cost model, repair policy, sampling, ablations, run artifacts, deployment surface, public write-up scope, and explicit out-of-scope items needed to make the benchmark publishable and reproducible.

## 2. Decisions

Fourteen decisions, made in sequence. Each is independently revisable.

### D1 — Eval unit type

The index emits two unit types, deterministically seeded at index-build time:

- **`pair_timeline`** — one `(src_ip, dst_ip)` conversation, in time order.
- **`host_egress`** — one `src_ip` together with multiple destinations within a time window. Used when malicious activity from a host fans out across many `dst_ip`s (scans, fan-out C2, exfil to multiple external IPs).

Assignment rule (at index time, from gold topology):

- If the malicious flows on a host are concentrated on a single destination → emit `pair_timeline` units.
- If a single `src_ip` has malicious flows to ≥ K distinct destinations within a time window → emit one `host_egress` unit instead of K separate `pair_timeline` units.

The model never chooses unit type. Investigation scope is open via tools, but the **scoring** boundary is the seeded `flow_id` set in the eval unit.

### D2 — Logical unit + per-provider rendering

Each `eval_unit_id` is provider-agnostic and references a stable, sorted set of `flow_id`s. For each provider we derive one or more **renderings**:

- A rendering is a contiguous time-ordered slice of the eval unit's flows.
- Each provider has a configurable `max_flows_per_rendering` (per dataset). Suggested starting values: Claude 600, GPT 1200, Gemini 3000.
- If the eval unit fits in one rendering, that rendering is the whole unit. Otherwise, sequential time-contiguous parts (`part_0`, `part_1`, ...). No random subsampling, ever.

Scoring is computed at the **`eval_unit_id`** level: predicted flow indices in each rendering are mapped back to global `flow_id`s via the rendering's flow-id list, then aggregated across renderings of the same logical unit before comparison to gold.

### D3 — Agent loop budget

Each rendering runs an agent loop with three bounds:

- **Fixed dollar cap per rendering** — same across all personas. Default `cost_usd_cap_per_rendering = 0.50`.
- **Per-persona turn / tool-call / wall-clock budgets** — reflects role complexity, not budget asymmetry, since the dollar ceiling is fixed.

Default values:

| Persona | max_turns | max_tool_calls | wall_clock_seconds |
|---|---|---|---|
| SOC Analyst | 4 | 6 | 60 |
| Threat Analyst | 8 | 12 | 120 |
| Adversary Hunter | 10 | 16 | 150 |
| Detection Engineer | 12 | 20 | 180 |

On overrun (any of: turns, tool calls, wall clock, dollar cap reached): inject a forced-final-answer message ("Budget reached. Submit your best assessment now.") and run one more turn. If that turn does not produce a valid `submit_assessment` JSON, the rendering is recorded as `final_valid=false` with `forced_final_answer=true`.

### D4 — Persona × tool matrix

Tools are pure-Python functions over the pre-built index. They are deterministic, in-process, and read-only. There is no MCP, no external service, no codegen.

| Tool | SOC | Threat | Hunter | DE |
|---|:--:|:--:|:--:|:--:|
| `list_pairs(filter, sort, limit)` | ✓ | ✓ | ✓ | ✓ |
| `get_pair_timeline(pair_id, offset, limit)` | ✓ | ✓ | ✓ | ✓ |
| `get_flows(flow_ids)` | ✓ | ✓ | ✓ | ✓ |
| `host_rollup(host)` | ✓ | ✓ | ✓ | ✓ |
| `top_destinations(host, limit)` | — | ✓ | ✓ | ✓ |
| `pair_stats(pair_id)` | — | ✓ | ✓ | ✓ |
| `port_proto_matrix(scope)` | — | — | ✓ | ✓ |
| `rarity_stats(scope)` | — | — | ✓ | ✓ |
| `submit_assessment(...)` | ✓ | ✓ | ✓ | ✓ |

Each tool returns identical schema regardless of persona. All tools have a `max_results` parameter capped at the harness layer to bound prompt growth. The combined tool implementations and JSON schemas are hashed into `tools_manifest_sha`, recorded in every run.

### D5 — Playbooks

Each persona has a versioned playbook composed at compile time alongside the persona prompt and the output contract. Playbooks contain **process and generic patterns only** — never IOC values, never attack family names that appear in dataset labels, never dataset-specific statistics, never "if X then malicious" rules.

Layout:

```
prompts/
├── shared/
│   ├── output_contract_v1.json
│   └── playbook_common_v1.md       # forbidden list, tool-use discipline, output discipline
├── personas/
│   └── <persona>_v1.md             # role + frame
└── playbooks/
    ├── soc_analyst_v1.md
    ├── threat_analyst_v1.md
    ├── adversary_hunter_v1.md
    └── detection_engineer_v1.md
```

The compose pipeline assembles the system prompt as:

```
system_scaffold + output_contract + persona + playbook_common + playbook_<persona> + tool_schemas
```

A regex check at compile time fails the build if any playbook contains forbidden tokens, where the forbidden list is the union of: attack family strings inferable from `schema.json` `label_inference`, a small malware-family denylist, and a literal IP/CIDR/hash pattern. The combined playbooks hash into `playbooks_manifest_sha`.

### D6 — Scoring

Three co-primary lenses, all computed against the eval unit's gold flow set:

- **Per-flow** — for each flow in the eval unit, compare predicted malicious membership to gold. Compute precision, recall, F1.
- **Per-IP-pair** — for each distinct `(src_ip, dst_ip)` in the eval unit, same comparison.
- **Per-host** — for each distinct host (used in `host_egress` units), same comparison.

One reliability indicator:

- **`unit_first_pass_valid`** — true iff every rendering of the eval unit returned a valid `submit_assessment` JSON within budget (`forced_final_answer=false`, `final_valid=true`). False otherwise.

Edge cases:

- `verdict=benign` with non-empty `malicious_flow_indices` → logged as `verdict_indices_mismatch`; flow indices used for per-flow metrics; verdict treated as benign for the chunk reliability note.
- `verdict=malicious` with empty `malicious_flow_indices` → same defect; per-flow recall counted as 0.

### D7 — Cost model

Three rollup layers, all persisted:

- **Per-call (`predictions_raw.jsonl`)** — one row per turn. Fields: `prompt_tokens`, `output_tokens`, `reasoning_tokens`, `cached_tokens`, `wall_time_ms`, `cost_usd`, plus identifiers (`eval_unit_id`, `rendering_id`, `provider`, `persona`, `turn_index`, `tool_name`, `tool_call_args_hash`, `tool_result_truncated`).
- **Per-rendering (`renderings.jsonl`)** — `rendering_id`, `eval_unit_id`, `provider`, `persona`, `turns_used`, `tool_calls_used`, `wall_time_ms`, `cost_usd`, `cap_hit` (bool), `cap_hit_reason` (`turns | tool_calls | wall_clock | cost`), `final_valid`, `forced_final_answer`.
- **Per-eval-unit (`eval_units_summary.jsonl`)** — aggregated cost across renderings of the unit, `unit_first_pass_valid`, predicted vs gold for the three scoring lenses.

`pricing.yaml` is shipped in the repo with `snapshot_date` and per-model rates. The system prompt + playbook + tool schemas are placed at the stable cacheable prefix; per-provider cache mechanism is declared in adapters (OpenAI automatic; Anthropic explicit `cache_control`; Gemini explicit cached content). `summary.json` reports a `cache` block with `hit_rate`, `savings_usd`, and per-provider breakdown.

The run-level config carries an optional `cost_budget_usd` guardrail; on breach the run stops cleanly and writes a partial summary.

### D8 — Repair policy

No client-side JSON repair. Provider-native structured output is declared in each adapter:

- **OpenAI Responses** — `text.format = { type: "json_schema", name: "submit_assessment", schema: <contract>, strict: true }`.
- **Anthropic Messages** — schema declared as a single forced tool with `tool_choice = {type: "tool", name: "submit_assessment"}`; tool input is the response body.
- **Gemini** — `response_mime_type = "application/json"` plus `response_schema = <contract>` (subset supported by Gemini schema).

Validation has two distinct moments:

- **Tool-call validation (recoverable)** — if the model emits a tool invocation that fails its schema, the harness returns `{"error": "schema_violation", "details": "..."}` to the model on the next turn. Logged as `tool_call_invalid`. Does **not** count against `unit_first_pass_valid`. The loop continues within the budget.
- **Final-answer validation (strict)** — if `submit_assessment` JSON fails validation, the rendering is `final_valid=false`. No repair attempt. Errors persisted in `predictions_raw.jsonl`.

A rendering that completed only via `force_final_answer` is excluded from `unit_first_pass_valid` even if its JSON parses, because it ran out of budget.

### D9 — Sampling

Stratified across `(unit_type, gold_label)` strata. Labels are derived at index time:

- `malicious` — eval unit has ≥ 1 malicious flow and ≥ 80% malicious flows.
- `benign` — eval unit has 0 malicious flows.
- `mixed` — anywhere in between.

Strata: `{pair_timeline, host_egress} × {malicious, benign, mixed}` → up to 6 strata.

| Mode | Default | Sampling | Override |
|---|---|---|---|
| smoke | 1 unit per non-empty stratum, min 8 units total | stratified random per stratum, seeded by `sample_seed` | `cost_budget_usd` (default `$10`) |
| full | 10 per stratum, capped at `full_unit_cap=60` | stratified random per stratum, seeded | `--run-all` bypasses cap; `cost_budget_usd` (default `$500`) still applies |

Empty strata are logged as `stratum_undersampled`, not failed. The combination `(dataset_hash, sample_seed, mode)` uniquely determines the eval unit set, and the same set is reused across ADR-0001 single-shot, ADR-0002 main, and all ADR-0002 ablations.

### D10 — Ablations

Mandatory in v1:

- **`tools_off`** — the persona's allowlist is reduced to `submit_assessment` only. Run on **smoke + full**.
- **`playbooks_off`** — the per-persona playbook is empty (only `playbook_common_v1` remains, so the forbidden list and output discipline still apply). Run on **smoke** only.
- **`adr0001_baseline`** — ADR-0001's single-shot run on the **same eval units**. This is the implicit `tools_off + playbooks_off + persona_off` baseline. Run on **full** when its full-mode artifacts exist.

Each ablation is a separate `run_id`. The `run_metadata.json` records the active ablation tag. All ablations on the same `(dataset_hash, sample_seed)` are joined by an aggregator under `ablations/<dataset_hash>/<seed>/ablation_summary.json`, which produces the headline deltas (`tools_off → main`, `playbooks_off → main`, `adr0001_baseline → main`).

Ablation runs do not share prompt cache because their stable prefix differs.

`persona_off` is deferred to v1.1 (would require a parallel persona-less prompt, output schema, and scoring axis).

### D11 — Run artifacts

All runs (both ADRs) live under a single flat `runs/<run_id>/` directory. The `run_metadata.adr_id` field discriminates ADR-0001 from ADR-0002 runs.

```
runs/<run_id>/
├── run_metadata.json
├── predictions_raw.jsonl
├── predictions_raw.parquet
├── renderings.jsonl
├── eval_units_summary.jsonl
├── tool_calls.jsonl
├── summary.json
├── prompts_used/
│   └── <persona>_<provider>.txt    # exact stable prefix snapshot for cache verification
└── index_manifest_link.json        # {"dataset_hash": "...", "index_uri": "..."}
```

Index artifacts are content-addressed and shared across runs:

```
indexes/<dataset_hash>/
├── manifest.json
├── flows.parquet
├── pairs.jsonl
├── hosts.jsonl
├── eval_units.jsonl
└── rollups/
    ├── hosts.parquet
    └── pair_stats.parquet
```

`run_id` format:

```
<UTC>_<host>_<manifest>_<mode>_<ablation_tag>

manifest      = sha256(prompts_manifest + playbooks_manifest + tools_manifest)[:8]
mode          = smoke | full
ablation_tag  = main | tools_off | playbooks_off | adr0001_baseline
```

`run_metadata.json` records `prompts_manifest_sha`, `playbooks_manifest_sha`, `tools_manifest_sha`, `pricing_snapshot_date`, `index_built_by_run_id`, `image_tag` (or `local-<git_sha>`), seed, mode, ablation, caps, and budget.

### D12 — Deployment

The benchmark ships as a pip-installable open-source repository, runnable locally from a Jupyter notebook or a CLI script. Local-first is the primary target.

Public-user contract:

- `pip install -e .` — single command, no external services to set up.
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` — env vars.
- `pricing.yaml` — shipped in repo; users update if rates drift.
- `benchmark_config.yaml` — shipped with safe defaults: smoke `cost_budget_usd: 10`, full `cost_budget_usd: 500` with confirm prompt, fixed `cost_usd_cap_per_rendering: 0.50`.
- A tiny sample dataset (≤ 10 MB) is committed to the repo so smoke can run with no downloads.
- Pre-built indexes for full datasets are mirrored to a public GCS bucket; users can rebuild via `python build_index.py` or download via `gsutil`.
- `notebooks/quickstart.ipynb` runs a smoke and plots one chart.

Optional scale-out targets:

- `k8s/gke-full-job.yaml`, `k8s/gke-cron.yaml` — for teams running batch full mode in GCP.
- `k8s/eks-full-job.yaml` — kept as a reference manifest for AWS users.
- `Dockerfile` — single image used by both K8s targets; same image as local for reproducibility.

The harness enforces the cost cap and turn budgets in code; K8s `activeDeadlineSeconds` is belt-and-suspenders only.

### D13 — Public write-up scope

Each public release includes:

- **`docs/adr/0002-LLMs-as-SOC-agents.md`** — methodology (this ADR).
- **`RESULTS.md`** — per-dataset section; each section has F1 / cost / reliability tables per (provider, persona, stratum) for `main`, `tools_off`, `playbooks_off`, and `adr0001_baseline`; ablation deltas.
- **`RESULTS_REPRODUCE.md`** — exact CLI commands and config values used for every row in `RESULTS.md`.
- **`notebooks/results_explorer.ipynb`** — loads `summary.json` from a published run and lets readers slice by stratum / persona / provider.
- **`pricing.yaml` snapshot** — referenced by every published number via `pricing_snapshot_date`.

Every published number is tagged with `prompts_manifest_sha`, `playbooks_manifest_sha`, `tools_manifest_sha`, `pricing_snapshot_date`. A change in any of those produces a new entry in `RESULTS.md`, never an in-place update.

An auto-updated leaderboard is deferred to v1.1.

### D14 — Out of scope

See §6.

## 3. Consequences

### Positive

- One reproducible methodology for benchmarking frontier LLMs as SOC agents on raw NetFlow, runnable from a laptop on a $10 budget.
- Separation of logical eval units (provider-agnostic) from renderings (per-provider) lets each model run at its strongest input size without breaking cross-provider comparison.
- Pre-indexed corpus + pure-Python tools means tools are deterministic, fast, and easy to audit; no MCP, no external services.
- Three-layer cost rollup with `cached_tokens` first-class makes the headline cost number defensible and lets users see where dollars went.
- Mandatory ablations attach attribution to every published number — `tools_off → main` and `playbooks_off → main` deltas explain how much of the lift comes from each layer.
- Forbidden-token regex check at compile time prevents playbooks from drifting into hidden hints.

### Negative / accepted trade-offs

- The benchmark measures **agent stack performance**, not raw model reasoning. A model that loses on Track 2 might still reason well; tool-use orchestration is part of what we score.
- Per-provider renderings of the same eval unit see different token counts; cross-provider cost comparison must be at the eval-unit rollup, never per-rendering.
- Tool authorship inevitably encodes prior — generic aggregations and rarity stats are still tools we built. The forbidden list mitigates the worst forms; an absolute neutrality guarantee is not possible.
- Per-persona turn / tool budgets introduce a controlled asymmetry across personas. The fixed dollar cap bounds it, but persona-specific F1 differences reflect both prompt design and budget.
- OSS distribution introduces user cost-overrun risk. Mandatory `cost_budget_usd` and shipped low-default smoke budget mitigate; users running `--run-all` on Opus full mode without raising the budget will still hit the guardrail before runaway spend.
- LogLM is not in this harness. Cross-comparison depends on running ADR-0001 on the same `(dataset_hash, sample_seed)`. Failing to do so means ADR-0002 numbers stand alone with no LogLM context.

## 4. Run artifacts

Per-run artifact set is fully described in D11. Aggregated ablation artifacts:

```
ablations/<dataset_hash>/<seed>/
├── adr0002_main_run_id.txt
├── adr0002_tools_off_run_id.txt
├── adr0002_playbooks_off_run_id.txt
├── adr0001_baseline_run_id.txt
└── ablation_summary.json
```

`ablation_summary.json` reports per (provider, persona, lens) deltas for each ablation against `main`, plus aggregate reliability and cost deltas.

## 5. Stage decoupling

The pipeline has three CLI entrypoints:

- **Stage A — `build_index.py`** — read parquet (lazy via DuckDB or Polars `scan_parquet`), normalize via `schema.json`, sort flows globally by `ts_start`, assign stable `flow_id`s, derive eval units (`pair_timeline`, `host_egress`), compute rollups, write `indexes/<dataset_hash>/`. Idempotent: a run with an existing `dataset_hash` is a no-op unless `--rebuild` is passed.
- **Stage B — `run_benchmark.py`** — read `indexes/<dataset_hash>/`, sample eval units per D9, render per provider per D2, run agent loop per D3, write `runs/<run_id>/` per D11. Skips Stage A entirely if the index for the active config exists.
- **Stage C — `aggregate_ablations.py`** — read all `runs/` entries with the same `(dataset_hash, sample_seed)`, join by ablation tag, write `ablations/<dataset_hash>/<seed>/ablation_summary.json` per D10. Cheap; safe to re-run.

All three accept the same `benchmark_config.yaml`. `--stage a|b|c|all` selects.

## 6. Other Considerations

Items explicitly out of scope for this revision but tracked here for follow-up.

### Companion tracks (future ADRs)

- **Capture-level eval unit (`capture_full`).** A third unit type that scores an entire small capture as one investigation. Useful for CTU-style captures that fit in a single rendering on the largest provider. Deferred until we measure how often captures fit; risk is a fragile size criterion that silently switches behavior.
- **Multi-agent collaboration on the same eval unit.** Persona handoff (Triage → Investigator → Detection Engineer) on a shared unit. Closer to a production SOC platform like Vigil. Deferred because it confounds detection ability with multi-agent orchestration design.
- **Live SIEM-backed agent benchmark.** Same agent stack pointed at real telemetry instead of parquet, with rate-limited Splunk / Elastic queries instead of in-process tools. Deferred because tooling becomes vendor-specific.

### Deferred design choices

- **Vector / semantic retrieval over pair-level summaries.** Optional `semantic_search` tool over precomputed pair summaries. Deferred because the embedding model becomes a versioned dependency, and structured retrieval (filters, sorts, rarity) is sufficient for v1 detection on NetFlow.
- **`persona_off` ablation.** Adds a parallel persona-less prompt, output block, and scoring axis. Deferred because designing a persona-less output that still scores against the same lenses is a separate exercise.
- **Stratification beyond `(unit_type, gold_label)`.** Stratification by attack family or capture size would give finer-grained smoke coverage. Deferred because attack-family stratification injects dataset-specific knowledge.
- **Cost-aware adaptive sampling.** Run fewer eval units of expensive providers under budget pressure. Adds metric complexity; defer until baseline `$ / correct flow` numbers are known.
- **Confidence-calibrated reliability metric.** Treat `unit_first_pass_valid` as continuous via confidence calibration. Deferred until we have a few full-mode runs to calibrate against.
- **Per-call prompt-cache reuse across runs.** Same persona prompt across many runs could share cache prefix on Anthropic / OpenAI, dropping cost further. Out of scope until first full-run cost report.

### Explicit exclusions

- **Agent-authored / codegen tools.** Out of scope for this benchmark in any version. Non-determinism, safety, scoring complexity. May appear as a separate qualitative DE study, never as part of headline F1.
- **LogLM in the ADR-0002 harness.** LogLM is evaluated under ADR-0001's raw single-shot methodology on the same eval units. Mixing LogLM into the agent track changes what's being measured.
- **MCP-based tool hosting.** In-process Python tools are simpler, deterministic, and benchmark-friendly. MCP is a fine integration story for product platforms; not for this benchmark.
- **Real-time / streaming benchmark.** Batch only. Datasets are file-based parquet.
- **Approval / response actions.** Read-only investigation. No `isolate_host`, no `block_ip`, no IR workflow.
- **Provider self-routing or sub-model selection.** Each provider is one model per run, declared in config. Cross-provider parity requires identical model identity across personas within a run.

## 7. Open follow-ups (tracked separately, not blocking)

1. Author `playbook_common_v1.md` and the four per-persona playbooks; extend `compose.py` with the forbidden-token regex check and the new compose order.
2. Implement `build_index.py` (Stage A): canonical `flows.parquet`, `pairs.jsonl`, `hosts.jsonl`, `eval_units.jsonl`, `rollups/`, content-addressed under `dataset_hash`.
3. Implement the read-only tool layer in `tools/` (in-process DuckDB / Polars queries), with shared JSON schemas declared once and adapted per provider.
4. Wire provider adapters: OpenAI Responses with `text.format`, Anthropic forced tool, Gemini `response_schema`. Place stable prefix at the cacheable boundary; declare per-provider cache mechanism.
5. Implement the agent loop in `run_benchmark.py` with per-rendering caps, force-final-answer behavior, three-layer cost rollups.
6. Implement `aggregate_ablations.py` and a minimal ablation summary schema.
7. Ship `notebooks/quickstart.ipynb`, `notebooks/results_explorer.ipynb`, `pricing.yaml`, sample dataset, public-bucket index mirror script.
8. Author `RESULTS.md` skeleton and `RESULTS_REPRODUCE.md`.
9. Optional: `k8s/gke-full-job.yaml`, `k8s/gke-cron.yaml`, `Dockerfile` for batch users.

## 8. Decision log (one-liners)

| ID  | Decision |
|-----|---------|
| D1  | Two unit types: `pair_timeline` + `host_egress`, deterministically seeded at index time. |
| D2  | Logical eval_unit + per-provider time-contiguous renderings; caps per dataset; scoring at logical-unit level. |
| D3  | Fixed `cost_usd_cap_per_rendering` + per-persona turn/tool/wall-clock budgets; force-final-answer on overrun. |
| D4  | Persona-scoped read-only tool allowlist; pure-Python in-process tools; `tools_manifest_sha` versioned. |
| D5  | Per-persona playbooks + shared `playbook_common_v1`; forbidden-token regex check at compile. |
| D6  | Per-flow + per-IP-pair + per-host F1 co-primary; `unit_first_pass_valid` reliability indicator. |
| D7  | Three-layer cost rollup; `cached_tokens` captured first-class; cache savings reported in summary. |
| D8  | Strict final-answer validation; recoverable tool-call errors; no client-side repair; force-final excluded from first-pass valid. |
| D9  | Stratified sampling on `(unit_type, label)` + cost guardrail; cross-ADR identical unit sets via shared seed. |
| D10 | `tools_off` (smoke + full) + `playbooks_off` (smoke) mandatory; ADR-0001 baseline = implicit triple-off. |
| D11 | Flat `runs/<run_id>/` for both ADRs; shared `indexes/`; new artifacts: renderings, eval_units_summary, tool_calls, prompts_used. |
| D12 | Local-first OSS distribution (CLI + notebook); GKE/EKS optional; smoke defaults to `cost_budget_usd: 10`. |
| D13 | Methodology + headline + per-stratum/per-persona breakdowns + ablation deltas + reproduce notes + explorer notebook. |
| D14 | §6 — companion tracks / deferred design choices / explicit exclusions. |

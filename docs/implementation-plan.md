# socbench — implementation plan

> Tracks the staged build-out of the socbench SOC-agent benchmark. Each step
> has explicit deliverables, exit criteria, and revision notes. Steps 1–3 land
> in the first implementation pass; Step 4 and Step 5 followed as separate
> units; Step 6 is scoring/sampling/ablations; Step 7 is polish.

## Stack baseline

- **Language**: Python ≥ 3.11
- **Packaging**: `uv` for development workflows; `pip install -e .` MUST work for OSS users.
- **Build backend**: `hatchling` 1.29+
- **Data**: Polars (lazy/eager DataFrames) + DuckDB (SQL on parquet) + pyarrow (zero-copy bridge)
- **Models**: pydantic v2 (all wire/state contracts)
- **CLI**: `click`
- **Logging**: stdlib `logging` with JSON formatter (`python-json-logger`)
- **Test**: `pytest`
- **License**: Apache-2.0
- **Layout**: `src/`-layout, package name `socbench`, distribution name `socbench`

## Status — first implementation pass

| Step | Status |
|---|---|
| 1. Skeleton & contracts | **done** — pyproject, schema, models, configs, logging, hashing, CLI surface |
| 2. Index builder `build-index` | **done** — normalize → flow_ids → eval_units (K=10 default) → rollups → content-addressed write |
| 3. Tools layer & sample builder | **done** — 9 tools + persona allowlist + tools_manifest_sha + sample-from-real script |
| 4. Prompts, playbooks, compose | **done** — 9 markdown content files under `config/prompts/`, single `prompts.py` module, forbidden-token check, `prompts_manifest_sha` + `playbooks_manifest_sha` |
| 5. Provider adapters & agent loop | **done** — 4 adapters (openai/anthropic/gemini, all chub-fetched; mock always-on), `agent.py` (AgentLoop + Runner + cost & latency rollups + artifact writers), `socbench run` subcommand |
| 6. Scoring, sampling, ablations, aggregate | **done** — `scoring.py` (per-flow/pair/host P/R/F1 + defects, gold from `flows.parquet`), `sampling.py` (stratified sampler, deterministic in `(dataset_hash, sample_seed, mode)`), `aggregate.py` (ablation joiner → `ablation_summary.json`), inline scoring + `scoring`/`cache` blocks in `summary.json`, `socbench aggregate` subcommand, stratified sampling as the `run` default |
| 7. Notebooks & docs polish | **done** — `notebooks/quickstart.ipynb` (self-contained: synthesizes a sample, runs the full mock pipeline, plots per-persona F1) + `notebooks/results_explorer.ipynb` (slices any run by stratum/persona/provider + ablation deltas), `RESULTS.md` / `RESULTS_REPRODUCE.md` skeletons, README brought current |

Verified locally: `pip install -e ".[dev]"` succeeds in a clean venv with
Python 3.13, `socbench --help` lists all subcommands, `pytest` returns
green, `ruff check src tests` returns clean, and the full
`build-index → run main → run tools_off → aggregate` loop produces, per
run, the complete artifact tree plus a `summary.json` carrying the
`scoring` block (macro per-flow/pair/host F1, precision/recall,
`first_pass_valid_rate`, `defect_count`) and the `cache` block
(`hit_rate`, `savings_usd`, per-provider), and an
`ablations/<dataset_hash>/<seed>/ablation_summary.json` with
`tools_off → main` deltas — all in < 3 seconds on the synthetic sample.

Step 6 simplifications (kept flat per the running directory-discipline note):
scoring/sampling/aggregate are three single-file modules, not subpackages;
scoring runs inline in the `Runner` (the only consumer besides the index
builder allowed to read ground truth) rather than as a separate pass;
`single_shot_baseline` is wired into the aggregator structurally but stays
inert until single-shot baseline runs exist; `predictions_raw.parquet` is
deferred to Step 7.

## Step 1 — Package skeleton & contracts

**Deliverables**

- `pyproject.toml` (PEP 621 metadata, hatchling backend, `socbench` console script, optional extras `[providers]`, `[dev]`, `[notebooks]`)
- `LICENSE` (Apache-2.0), `README.md`, `.gitignore`, `.python-version`
- `config/benchmark_config.yaml` (sampling/dataset defaults + persona × tool matrix), `config/pricing.yaml` (snapshot-dated)
- `config/schema.json` (canonical NetFlow + normalization aliases + label inference; rewritten from scratch)
- `src/socbench/` skeleton (after consolidation, files at module level rather than as subpackages):
  - `__init__.py`, `_version.py`
  - `config.py`: typed loaders for `config/benchmark_config.yaml` and `config/pricing.yaml`; sibling-relative path resolution
  - `schema.py`: canonical record model + alias resolver + label inference
  - `models.py`: pydantic v2 models for `Flow`, `Pair`, `Host`, `EvalUnit`, `Rendering`, `ToolCall`, `SubmitAssessment`, `RenderingResult`, `EvalUnitSummary`, `RunMetadata`
  - `hashing.py`: deterministic content hashing (blake2b) + manifest hashing
  - `logging_config.py`: structured logger
  - `cli.py`: registers `build-index`, `run`, `aggregate`, `tools-smoke` subcommands (later steps implement them)
- `tests/`: contract round-trip tests for every pydantic model; schema normalizer tests

**Exit criteria**

- `python -m venv .venv && .venv/bin/pip install -e .` succeeds
- `uv venv && uv pip install -e ".[dev]"` succeeds
- `socbench --help` lists all subcommands
- `pytest` is green

## Step 2 — Index builder: `build-index` (corpus index)

**Deliverables**

- `src/socbench/index.py` (`normalize_parquet`): DuckDB-backed parquet read; column alias resolution from `config/schema.json`; label inference; emits a normalized `polars.DataFrame`
- `src/socbench/index/flow_ids.py`: globally sort by `(ts_start, src_ip, dst_ip, src_port, dst_port, protocol)` for tie-breaking determinism, assign monotonic `uint64` `flow_id`
- `src/socbench/index/pairs.py`: derive `(src_ip, dst_ip)` pair index → `pairs.jsonl`
- `src/socbench/index/hosts.py`: derive per-host index → `hosts.jsonl`
- `src/socbench/index/eval_units.py`: eval-unit assignment rule
  - `host_egress` when a `src_ip` reaches ≥ `K` distinct `dst_ip`s (label-agnostic — never derived from ground truth) within window `W` (default `K=10`, `W=5min`, both configurable); else `pair_timeline` per `(src_ip, dst_ip)`
  - both unit types are split into contiguous, time-ordered sub-windows of at most `max_flows_per_unit` flows (default `1000`) so each unit stays within a model's effective context window; the split is by time, never by destination, so the scope the model perceives matches the scope it is graded on
  - stable `eval_unit_id`s derived from the flow-id set hash
- `src/socbench/index/rollups.py`: `hosts.parquet`, `pair_stats.parquet`
- `src/socbench/index/manifest.py`: computes `dataset_hash` from canonical normalized payload (not raw bytes — same logical data → same hash)
- `src/socbench/index/store.py`: writes content-addressed `indexes/<dataset_hash>/` tree
- `src/socbench/cli/build_index.py`: wires `socbench build-index --config <path> [--rebuild]`; idempotent; honours `--rebuild`
- `tests/synthetic_flows.py`: test-only deterministic flow generator
- `tests/test_index_*.py`: normalize, flow_ids, eval_units (assignment rule), rollups, manifest stability

**Exit criteria**

- Two `build-index` invocations on the same input emit the same `dataset_hash`
- `--rebuild` regenerates artifacts; absence of `--rebuild` is a no-op when the index exists
- Synthetic mixed-traffic fixture produces correct `pair_timeline` vs `host_egress` assignment

## Step 3 — Tools layer & sample dataset

**Deliverables**

- `src/socbench/tools/base.py`: `Tool` ABC; signature `(args: dict, ctx: ToolContext) -> dict`; built-in JSON Schema validation on `args` and on returned payload
- `src/socbench/tools/registry.py`: persona allowlist enforcement, `max_results` cap, hashable manifest
- `src/socbench/tools/schemas/`: one JSON schema per tool (also embedded in the manifest hash)
- `src/socbench/tools/impl/`: the nine tools
  - `list_pairs`, `get_pair_timeline`, `get_flows`, `host_rollup`, `top_destinations`, `pair_stats`, `port_proto_matrix`, `rarity_stats`, `submit_assessment` (terminal action)
- `src/socbench/tools/manifest.py`: `tools_manifest_sha`
- `scripts/build_sample_from_real.py`: takes path or GCS URI to `NF-CSE-CIC-IDS2018.parquet`, downsamples to ≤ 10 MB with stratified mixed labels, writes to `data/sample/cic2018-mini.parquet`, emits `data/sample/PROVENANCE.md`
- `data/sample/README.md` placeholder explaining how to regenerate
- `src/socbench/cli/tools_smoke.py`: `socbench tools-smoke --index <dataset_hash>` runs every tool against the index, prints a per-tool summary
- `tests/test_tools_*.py`: one test per tool against the synthetic-flow-built index

**Exit criteria**

- All nine tools have schemas + impls + tests
- Persona allowlist denies disallowed tools with `tool_call_invalid`
- `tools_manifest_sha` is stable across runs
- Sample-build script runs locally against a provided source parquet and produces a ≤ 10 MB output

---

## Step 4 — Prompts, playbooks, compose pipeline

**Deliverables (shipped, simplified vs. the original plan to keep the tree flat)**

- `config/prompts/playbook_common.md` — shared playbook, persists under `playbooks_off`
- `config/prompts/personas/<persona>.md` × 4 — role/frame block per persona
- `config/prompts/playbooks/<persona>.md` × 4 — per-persona playbook, dropped under `playbooks_off`
- `src/socbench/prompts.py` — single module (no subpackage) exposing:
  - `load_prompts(prompts_dir) → PromptParts`: filename-stem-matched persona/playbook reader
  - `compose(parts, persona, ablation, output_contract_schema, tool_schemas, label_inference) → str`: assembles in the required compose order and runs the forbidden-token check on the assembled string before returning
  - `check_forbidden_tokens(text, …)` / `ForbiddenTokenInPrompt`: regex check built from `GROUND_TRUTH_FIELDS` + `schema.label_inference.{attack_columns, label_columns, attack_family_strings_used_for_forbidden_token_check}` + IPv4/IPv6/hex-hash literal patterns
  - `prompts_manifest_sha(parts)` / `playbooks_manifest_sha(parts, ablation)`: separate hashes so `playbooks_off` rotates one without disturbing the other
- `tests/test_prompts.py` — 25 cases covering load, compose determinism, every persona, `playbooks_off` behaviour, forbidden-token positives/negatives, shipped-content cleanliness, and manifest stability/rotation
- Output contract is loaded from `SubmitAssessmentTool.args_schema` (Step 3) rather than duplicated as a JSON file

**Deviations from the plan-as-written (all to keep the tree flat per repo convention)**

- Content lives under `config/prompts/` instead of a top-level `prompts/` dir (matches existing `config/{schema.json, pricing.yaml, benchmark_config.yaml}` consolidation)
- `_v1` filename suffix dropped — the manifest hash IS the version, and any content edit rotates it automatically (consistent with the append-only RESULTS.md rule)
- `compose.py` collapsed into a single `prompts.py` module — consistent with `config.py`, `models.py`, `schema.py`, `index.py`, `cli.py` all being single-module
- `output_contract.json` not maintained as a separate file — loaded from `SubmitAssessmentTool.args_schema` so the two cannot drift apart
- Step 5 writes the compiled snapshots to `runs/<run_id>/prompts_used/<persona>_<provider>.txt` as part of the agent loop's per-run bookkeeping

**Exit criteria (met)**

- All four personas compose successfully under both `main` and `playbooks_off`
- Shipped content passes the forbidden-token check (covered by `test_shipped_content_passes_check`)
- `playbooks_manifest_sha` rotates between `main` and `playbooks_off`; `prompts_manifest_sha` is invariant
- `pytest` green; `ruff check src tests` clean

## Step 5 — Provider adapters & agent loop

**Deliverables (shipped, simplified vs. the original plan to keep the tree flat)**

- `src/socbench/providers/` — one file per adapter under a subpackage (the only multi-file area; each SDK's translation logic is genuinely different):
  - `base.py`: `Adapter` ABC, `AdapterRequest` / `AdapterResponse` / `TokenUsage` / `AdapterToolCall` / `Message`, error hierarchy (`RetryableAdapterError` vs `FatalAdapterError`), and a lazy `build_adapter(name, model)` factory that imports each provider's SDK only on first use
  - `mock_adapter.py` — deterministic scriptable adapter; default 2-step script (tool_call → submit) plus `MockAdapter.with_script(...)` for tests; honors `force_final_answer` regardless of script state; supports `reset()` so one instance can drive many renderings
  - `openai_adapter.py` — Responses API (`client.responses.create`), tools surfaced as function tools, `tool_choice={"type":"function","name":"submit_assessment"}` to force final answer; usage extraction surfaces `cached_tokens` from `input_tokens_details.cached_tokens` and `reasoning_tokens` from `output_tokens_details.reasoning_tokens`. Errors mapped: `RateLimitError`/`APITimeoutError`/`APIConnectionError` → retryable; `APIStatusError` 5xx → retryable, else fatal
  - `anthropic_adapter.py` — Messages API with `system=[{type:text, text, cache_control:{type:ephemeral}}]` to engage Anthropic prompt-cache; `tool_choice={"type":"tool","name":"submit_assessment"}` to force final answer; `usage.cache_read_input_tokens` surfaces as `cached_tokens`
  - `gemini_adapter.py` — `google-genai` SDK via `client.models.generate_content(...)`; tools wrapped in `types.Tool(function_declarations=[...])`; force-final uses `tool_config.function_calling_config={mode:'ANY', allowed_function_names:['submit_assessment']}`; `usage_metadata.cached_content_token_count` / `thoughts_token_count` surface as `cached_tokens` / `reasoning_tokens`
- `src/socbench/agent.py` — single module (loop + cost + runner + artifact writers + summary):
  - `AgentLoop.run(unit)`: loop budgets (turns / tool_calls / wall_clock / cost), forced-final-answer message injection, recoverable tool-call dispatch (`ToolSchemaViolation` → recoverable error to model), strict pydantic validation on the final `submit_assessment`
  - `Runner.run(units)`: orchestrates `(unit × persona × provider)` renderings, writes `predictions_raw.jsonl`, `renderings.jsonl`, `eval_units_summary.jsonl`, `tool_calls.jsonl`, `run_metadata.json`, `prompts_used/<persona>_<provider>.txt`, `index_manifest_link.json`, and a `summary.json` with per-(provider, persona) cost + latency rollups (p50/p95/max + first_pass_valid_count)
  - `compute_summary(...)`: latency percentiles per-(provider, persona) from `predictions_raw.jsonl`; cost + wall-time aggregated from `EvalUnitSummary`
  - `generate_run_id(...)`: human-readable + collision-resistant (`<UTC>_<mode>_<ablation>_<providers>_<short_hash>_<uuid6>`)
- `src/socbench/cli.py` — `run` subcommand wired with `--providers all|csv` (`all` resolves to enabled-in-config), `--personas all|csv`, `--unit-id` xor `--limit`, `--ablation main|tools_off|playbooks_off`, `--cost-budget-usd` override, `--mode smoke|full`
- `src/socbench/models.py` — added `PredictionRow` (typed wire shape for `predictions_raw.jsonl`) and `EvalUnitSummary.wall_time_ms` (latency aggregate)
- `config/pricing.yaml` — renamed provider key `google → gemini` so `pricing.providers[name]` looks up by the same name used in `benchmark_config.providers`
- `tests/` — `test_providers_mock.py` (9 cases), `test_agent_loop.py` (16 cases including Runner artifact writes and cost-budget abort), `test_cli_run.py` (7 cases including manifest rotation across ablations + structural lazy-import test for all 3 real adapters)

**Deviations from the plan-as-written (all to keep the tree flat per repo convention)**

- `src/socbench/agent/` subpackage collapsed to a single `agent.py` (consistent with `config.py`, `models.py`, `prompts.py`, `index.py`, `cli.py`); cost rollup is ~40 LOC and doesn't justify its own module
- `src/socbench/cli/run.py` subpackage collapsed into the existing single-file `cli.py`
- Unit selection is `--unit-id ID` xor `--limit N` (first N by sorted id); stratified sampling lands cleanly in Step 6 without disturbing the agent loop
- Multi-part rendering split (when `flow_count > rendering_caps_per_provider`) deferred: Step 5 errors if a unit exceeds the cap. Splitter is its own routine and lands in Step 5b or 6
- `predictions_raw.parquet` deferred — Step 5 only emits `predictions_raw.jsonl`; Step 6 converts when aggregating
- A minimal `summary.json` ships in Step 5 (cost + latency rollups only); Step 6 adds the scoring lenses to it
- Real-provider live-API tests are out of scope; the structural lazy-import test (`tests/test_cli_run.py::test_real_adapter_imports_lazily_and_errors_without_sdk`) covers the no-SDK path, and the chub-fetched adapter code is the SDK-shape source of truth

**chub-first rule (followed):** docs fetched via `chub get openai/package`, `chub get anthropic/package`, `chub get gemini/genai` (all `--lang py`) before each adapter was written. SDK pin discovered from chub: `openai==2.26.0` (Responses API, `gpt-5.5` default), `anthropic==0.84.0` (Messages API, prompt-cache via `cache_control`), `google-genai==1.56.0` (NOT the deprecated `google-generativeai`).

**Exit criteria (met)**

- `socbench run --providers mock --personas all --limit 3` produces 12 renderings with the full artifact tree in < 2 seconds
- `prompts_manifest_sha` is invariant across ablations; `playbooks_manifest_sha` rotates only under `playbooks_off`
- All 4 real adapters refuse cleanly without their SDKs installed (no bare `ImportError`)
- `pytest` green; `ruff check src tests` clean

## Step 6 — Scoring, sampling, ablations, aggregate

Built flat (single-file modules), not as the `scoring/`, `sampling/`,
`ablations/`, `artifacts/` subpackages originally sketched — consistent with
the directory-discipline note carried since Step 1.

- `src/socbench/scoring.py`: per-flow / per-IP-pair / per-host
  precision/recall/F1 + `verdict_indices_mismatch` defect detection; gold
  is read from the index `flows.parquet` via DuckDB (`load_gold` →
  `GoldIndex`); predictions are clamped to the unit's seeded flow set.
- `src/socbench/sampling.py`: stratifier over `(unit_type, gold_label)`;
  deterministic in `(dataset_hash, sample_seed, mode)`; smoke tops up to
  `min_total_units`, full caps at `full_unit_cap`; empty strata reported as
  `stratum_undersampled`.
- `src/socbench/aggregate.py`: ablation joiner — reads runs sharing
  `(dataset_hash, sample_seed)`, picks the latest run per ablation tag,
  writes `ablations/<dataset_hash>/<seed>/ablation_summary.json` with
  `tools_off → main` / `playbooks_off → main` / `single_shot_baseline → main`
  deltas plus the per-ablation `run_id` pointer files.
- `agent.py`: the `Runner` scores each unit inline (filling the
  `EvalUnitSummary` lens fields + `verdict` + `defect`); `compute_summary`
  emits the `scoring` block (macro F1 + reliability + defect counts) and the
  `cache` block (`hit_rate`, `savings_usd`, per-provider) in
  `summary.json`.
- `cli.py`: `socbench aggregate --dataset-hash H [--seed N]`; `run` now
  defaults to stratified sampling, with `--unit-id` / `--limit` as overrides.

## Step 7 — Notebooks, README, docs polish

- `notebooks/quickstart.ipynb` — self-contained and key-free: synthesizes a
  tiny NetFlow parquet inline, runs `build-index → run --providers mock
  --personas all` via the CLI, loads `summary.json`, and plots per-persona F1
  by lens. Verified end-to-end with `nbconvert --execute`.
- `notebooks/results_explorer.ipynb` — loads any `runs/<run_id>/` (auto-picks
  the latest by default), renders the `scoring` / cost / latency / `cache`
  tables, a per-stratum `(unit_type, gold_label)` breakdown from
  `eval_units_summary.jsonl`, and the ablation deltas if an
  `ablation_summary.json` exists.
- `RESULTS.md` — published-numbers skeleton: provenance header (manifest SHAs
  + pricing snapshot), headline F1/cost/reliability table, per-stratum table,
  ablation-delta table, cache table. Marked "no published numbers yet"; new
  manifest/pricing combos add new dated sections rather than editing in place.
- `RESULTS_REPRODUCE.md` — exact CLI block per row type (build-index → run main
  → run ablations → aggregate) plus the provenance-extraction snippet.
- README refreshed: status table through Step 7, real `run` / `aggregate`
  quickstart steps, corrected layout (flat modules, `config/prompts/`,
  `providers/`), and updated "extending" instructions.

---

## Out of scope for this implementation

Deliberately excluded:

- K8s manifests, Dockerfile (user explicitly excluded for the local-only build)
- LogLM integration (the external single-shot baseline, maintained separately)
- MCP-based tool hosting
- Streaming / real-time benchmark
- Response / containment actions

## Future scope

- **Explicit per-step reasoning traces.** The agent loop is structured ReAct
  (reason → act → observe), but per-turn reasoning currently lives only in the
  transient `response.text` / provider extended-thinking; only the final
  `rationale` from `submit_assessment` is persisted (in
  `eval_units_summary.jsonl`). A future enhancement could capture and persist
  each turn's thought/scratchpad so we can score *how* a model reasons (e.g.,
  reasoning quality, dead-ends, tool-selection rationale), not just its final
  verdict. This is an analysis/observability enhancement and is orthogonal to
  the loop architecture — it requires no change to the ReAct control flow.

## Versioning policy

The repo follows SemVer for the **Python API** and reproducibility manifests
(`prompts_manifest_sha`, `playbooks_manifest_sha`, `tools_manifest_sha`,
`pricing_snapshot_date`). Any change to a manifest produces a new row in
`RESULTS.md`, never an in-place update.

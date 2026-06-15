# socbench

> Benchmark frontier reasoning LLMs as **SOC agents** on raw NetFlow data.

`socbench` benchmarks frontier reasoning models as SOC agents:
each model runs a bounded multi-turn agent loop against a deterministic,
pre-indexed NetFlow corpus, with persona-scoped read-only tools, fixed dollar
caps per investigation, and a strict final-answer JSON contract. Four personas
(SOC Analyst, Threat Analyst, Adversary Hunter, Detection Engineer) and three
providers (OpenAI, Anthropic, Google) share the same eval units, scoring
lenses, and ablation surface so the headline numbers and `tools_off` /
`playbooks_off` deltas are directly comparable.

The repository is **local-first**. A laptop, three API keys, and a sample
parquet committed to the repo are enough to reproduce a smoke under a $10
budget.

## Status

Alpha. The full pipeline runs end-to-end. Build-out covered:

- **Step 1**: package skeleton, contracts, configs, schema
- **Step 2**: the index builder (`socbench build-index`) with deterministic
  content-addressed indexes
- **Step 3**: read-only tools layer with persona allowlist + sample builder
- **Step 4**: personas, playbooks, prompt compose + forbidden-token check
- **Step 5**: provider adapters (OpenAI / Anthropic / Gemini + always-on
  mock) and the multi-turn agent loop with budget caps and cost/latency rollups
- **Step 6**: scoring (per-flow / per-pair / per-host F1), stratified
  sampling, ablation aggregation
- **Step 7**: quickstart + results-explorer notebooks; reproduction
  instructions in `REPRODUCE.md`

You can run a complete smoke today with **no API keys** via the mock provider
(see Quickstart step 3, or `notebooks/quickstart.ipynb`).

## Install

`socbench` ships as a standard PEP 621 / hatchling project. Either install
path works.

### With `uv` (recommended for development)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/DeepTempo/socbench.git
cd socbench

uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev,providers]"
```

### With plain `pip`

```bash
git clone https://github.com/DeepTempo/socbench.git
cd socbench

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,providers]"
```

Either way, `socbench --help` should now list the available subcommands.

## Configuration

| Surface | Default | Lives in |
|---|---|---|
| Benchmark defaults (sampling, agent budgets, providers, persona × tool matrix) | `benchmark_config.yaml` | `config/` |
| Canonical NetFlow schema + normalization aliases | `schema.json` | `config/` |
| Provider pricing snapshot (USD per 1M tokens) | `pricing.yaml` | `config/` |
| Provider API keys | env vars `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` | shell env |

`config/benchmark_config.yaml` ships safe defaults: smoke `cost_budget_usd: 10`,
full `cost_budget_usd: 900`, fixed `cost_usd_cap_per_rendering: 0.50`. Paths
inside it that point at sibling config files (`schema_path`, `pricing_path`)
resolve relative to the YAML's own directory, so renaming or relocating
`config/` doesn't require any code edits.

## Quickstart

### 1. Build a content-addressed index from a parquet dataset

```bash
socbench build-index \
  --config config/benchmark_config.yaml \
  --dataset sample
```

This normalizes the parquet against `config/schema.json`, sorts globally by
`ts_start` with deterministic tie-breaking, assigns stable `flow_id`s,
derives `pair_timeline` / `host_egress` eval units, computes
rollups, and writes to `indexes/<dataset_hash>/`.

Re-running the command on the same data is a no-op. Pass `--rebuild` to
force a rebuild.

### 2. Inspect the tool layer

```bash
socbench tools-smoke \
  --dataset-hash <dataset_hash> \
  --persona soc_analyst
```

This invokes every tool in the persona's allowlist against the built index
and prints a summary, with no model calls.

### 3. Run the benchmark

```bash
# Free, deterministic, no API keys (the mock provider):
socbench run --dataset-hash <dataset_hash> --providers mock --personas all

# Real models (after `pip install -e ".[providers]"` + exporting API keys):
socbench run --dataset-hash <dataset_hash> --providers all --personas all
```

Unit selection defaults to stratified sampling, deterministic in
`(dataset_hash, sample_seed, mode)`. Each `(unit × persona × provider)`
rendering runs a bounded multi-turn agent loop; results land under
`runs/<run_id>/` with `summary.json` (scoring + cost + cache rollups),
`eval_units_summary.jsonl`, `predictions_raw.jsonl`, `renderings.jsonl`,
`tool_calls.jsonl`, and `prompts_used/`.

### 4. Run ablations and aggregate the deltas

```bash
socbench run --dataset-hash <dataset_hash> --ablation tools_off --providers mock --personas all
socbench aggregate --dataset-hash <dataset_hash>
# → ablations/<dataset_hash>/<seed>/ablation_summary.json  (tools_off → main deltas)
```

### 5. Explore

`notebooks/quickstart.ipynb` runs the whole loop (it synthesizes a sample
dataset so it needs no committed data) and plots per-persona F1.
`notebooks/results_explorer.ipynb` loads any `runs/<run_id>/` and slices the
results by stratum, persona, and provider. Install with
`pip install -e ".[notebooks]"`.

## Extending the benchmark

Every interface designed to evolve is a registry or a YAML key:

- **New tool**: drop a new file under `src/socbench/tools/catalog/<name>.py`
  with a `Tool` subclass, register it in `src/socbench/tools/catalog/__init__.py`
  by appending to `ALL_TOOLS`, then add its name to the appropriate persona
  `tools:` lists in `config/benchmark_config.yaml`. The `tools_manifest_sha`
  shifts automatically. Filename, YAML name, and matrix entry are 1:1 by design.
- **New eval-unit type**: add an assigner to `src/socbench/index.py` and a
  matching `Literal` to `EvalUnitType` in `src/socbench/models.py`.
- **New provider adapter**: implement the `Adapter` ABC in a new
  `src/socbench/providers/<name>_adapter.py`, register it in the
  `build_adapter` factory in `providers/base.py`, and add an entry under
  `providers:` in `config/benchmark_config.yaml`. Pricing goes in
  `config/pricing.yaml`. SDK imports stay lazy so the dependency is optional.
- **New persona**: add a block under `agent.personas:` in
  `config/benchmark_config.yaml` with its budget and `tools:` allowlist.
- **New scoring lens**: add a lens to `score_unit` in `src/socbench/scoring.py`
  and a matching field to `EvalUnitSummary` in `models.py`.
- **New ablation**: extend the `Ablation` handling in `prompts.py` / `agent.py`
  and the tag list in `aggregate.py`.

## Methodology

The full methodology (eval units, persona x tool matrix, agent loop, scoring,
cost model, repair policy, sampling, ablations, run artifacts) is implemented
across the module-level files in `src/socbench/` (each carries a focused module
docstring).

## License

Apache-2.0. See [LICENSE](LICENSE).

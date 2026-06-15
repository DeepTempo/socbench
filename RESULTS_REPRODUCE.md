# Reproducing the numbers in RESULTS.md

Every row in [`RESULTS.md`](RESULTS.md) comes from the artifacts of a small set
of `socbench` invocations. This file lists the exact commands and the config
values they pin, so any reader can regenerate a section on their own API keys.

## 0. Environment

```bash
git clone https://github.com/DeepTempo/socbench.git
cd socbench
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[providers]"          # add ,dev,notebooks if you want those too

export OPENAI_API_KEY=...
export ANTHROPIC_API_KEY=...
export GOOGLE_API_KEY=...
```

The pinned models, budgets, and persona policies all live in
`config/benchmark_config.yaml`; pricing in `config/pricing.yaml`. A published
section is only valid for the `*_manifest_sha` / `pricing_snapshot_date` values
recorded in its provenance header. If you change a prompt, tool, playbook, or
rate, you produce a **new** section.

## 1. Build the index (Step A)

```bash
socbench build-index --config config/benchmark_config.yaml --dataset <dataset>
# → prints: dataset_hash=<HASH> flows=... pairs=... hosts=... eval_units=...
```

`build-index` is content-addressed and idempotent. Re-running on identical
input is a no-op; pass `--rebuild` to force. Record the printed `<HASH>`; it is
the reproducibility key shared by every run and ablation below.

## 2. Headline run (`main` ablation)

```bash
socbench run \
  --config config/benchmark_config.yaml \
  --dataset-hash <HASH> \
  --mode smoke \
  --ablation main \
  --providers all \
  --personas all
# → writes runs/<run_id>/ ; prints run_id + cost + run_dir
```

- `--mode smoke` uses the `sampling.smoke` config (1 unit / non-empty stratum,
  min 8); `--mode full` uses `sampling.full` (500 / stratum, cap 1500).
- `--providers all` runs every provider with `enabled: true` in config; use a
  CSV (`--providers openai,anthropic`) to run a subset, or `mock` for a free
  dry run.
- Unit selection defaults to stratified sampling (deterministic in
  `(dataset_hash, sample_seed, mode)`). `--unit-id` / `--limit` override it for
  debugging only; published rows always use the sampler.
- The smoke `cost_budget_usd` guardrail (default $10) stops the run cleanly if
  exceeded and writes a partial summary.

For a full multi-provider headline run with per-provider budget caps (the
shape of a published RESULTS.md row), use `scripts/run_full_benchmark.sh`
instead of calling `socbench run` directly. It launches one run per provider
in parallel, applying distinct `--cost-budget-usd` ceilings (currently $700
for openai and gemini, $900 for anthropic) so each provider stops at its own
console usage limit. The script reads `DATASET_HASH`, `MODE`, `ABLATION`, and
`PROVIDERS` from the environment; defaults are `MODE=full ABLATION=main
PROVIDERS=all`.

The **Headline**, **Per-stratum**, and **Cache** tables in `RESULTS.md` are read
from this run's `runs/<run_id>/summary.json` (the `scoring` and `cache` blocks)
and `eval_units_summary.jsonl` (per-stratum grouping).

## 3. Mandatory ablations

```bash
# tools_off: allowlist reduced to submit_assessment only (smoke + full)
socbench run --config config/benchmark_config.yaml --dataset-hash <HASH> \
  --mode smoke --ablation tools_off --providers all --personas all

# playbooks_off: per-persona playbook emptied; playbook_common still applies (smoke only)
socbench run --config config/benchmark_config.yaml --dataset-hash <HASH> \
  --mode smoke --ablation playbooks_off --providers all --personas all
```

Each ablation is its own `run_id` and does **not** share prompt cache with
`main` (the stable prefix differs).

## 4. Aggregate the ablation deltas (Step C)

```bash
socbench aggregate --config config/benchmark_config.yaml --dataset-hash <HASH>
# → writes ablations/<HASH>/<seed>/ablation_summary.json
```

The **Ablation deltas** table reads `tools_off → main`, `playbooks_off → main`,
and (when present) `single_shot_baseline → main` from this file. The aggregator
picks the most recent run per ablation tag and requires a `main` run to exist.

## 5. Fill in the provenance header

Read the manifest SHAs and pricing snapshot straight from the run:

```bash
python - <<'PY'
import json, glob
meta = json.load(open(sorted(glob.glob("runs/*_main_*/run_metadata.json"))[-1]))
for k in ("dataset_hash","sample_seed","mode","prompts_manifest_sha",
          "playbooks_manifest_sha","tools_manifest_sha","pricing_snapshot_date"):
    print(f"{k}: {meta[k]}")
PY
```

Copy those into the provenance header of the matching `RESULTS.md` section.

## Free, no-key dry run

To exercise the entire pipeline without API keys (deterministic mock provider):

```bash
socbench run --dataset-hash <HASH> --providers mock --personas all
```

or run [`notebooks/quickstart.ipynb`](notebooks/quickstart.ipynb), which also
synthesizes a sample dataset so no real data is needed.

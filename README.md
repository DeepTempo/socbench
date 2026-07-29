# SOCBench

> An open benchmark and harnesses for AI in cybersecurity operations.

> [!IMPORTANT]
> **NEW: PatchLoop.** On PatchEval (ByteDance), PatchLoop clears OpenHands, Claude Code, and SWE-agent by 20+ points: one verify-feedback round lifts strict score from 33.5% to 57.4% at ~$0.17/CVE.
> [See the harness](src/socbench/patchloop) · [How it works](https://socbench.org/patchloop/) · [Jump to section](#patchloop-vulnerability-detection-and-patching)

**What you can't measure, you can't improve. What you can't measure, you also
can't validate.** AI in cybersecurity operations is outpacing both: vendors
ship new agents faster than anyone can compare them on telemetry that
resembles what defenders actually see.

SOCBench is being built to be that benchmark: exhaustive and open, putting AI
systems through the actual work a SOC does and scoring them on the four
dimensions that decide whether anyone can ship them: **efficacy** (does it get
the right answer?), **cost** (what does it take to run?), **latency** (how
fast?), and **reliability** (well-formed output, every time, within budget).

Published results, interactive charts, and methodology live at
**[socbench.org](https://socbench.org)**. This repository contains the source
behind them: the benchmark harnesses, sample data, scoring pipeline, and the
website itself. SOCBench grew out of work DeepTempo does to validate and
improve their [LogLM](https://deeptempo.ai) and the open-source AI-SOC
[Vigil](https://github.com/Vigil-SOC/vigil). Everyone is welcome to contribute
and to help lead the project.

## Capabilities

SOCBench is organized around SOC capabilities. Each capability gets its own
harness. Two are live today; this repo hosts both.

| Capability | Status | Implementation |
|---|---|---|
| **Detection** | **Live** | The `socbench` Python package (this repo) · [results](https://socbench.org/detection/) |
| **Vulnerability detection & patching** | **Live** | [PatchLoop](src/socbench/patchloop), TypeScript · [results](https://socbench.org/patchloop/) |
| Triage & escalation | Roadmap | Rank alerts, dedupe, decide what gets paged and what gets closed |
| Investigation & DFIR | Roadmap | Multi-step reasoning over evidence; timelines, root cause |
| Threat hunting | Roadmap | Proactive search for adversary TTPs without a triggering alert |
| Detection engineering | Roadmap | Synthesize rules and queries, explain false positives |
| Threat intelligence | Roadmap | IOC enrichment, actor attribution, ATT&CK mapping |
| Response & remediation | Exploring | Playbooks, containment, comms; open question whether a clean benchmark is possible |

Live = published results. Roadmap = scoped, not yet measured. Exploring = open
question whether a clean benchmark is possible.

---

# Detection: the `socbench` package

`socbench` benchmarks reasoning models as SOC agents:
each model runs a bounded multi-turn agent loop against a deterministic,
pre-indexed NetFlow corpus, with persona-scoped read-only tools, fixed dollar
caps per investigation, and a strict final-answer JSON contract. Four personas
(SOC Analyst, Threat Analyst, Adversary Hunter, Detection Engineer) share the
same eval units, scoring lenses, and ablation surface, so headline numbers and
`tools_off` / `playbooks_off` deltas are directly comparable across systems.

The published run compares **seven systems** on 1,205 network-flow eval units:
three frontier LLMs (Claude Opus 4.7, GPT-5.4, Gemini 2.5 Pro), three
open-weights models (Foundation-Sec-8B, Seneca-QwQ-32B, GLM 5.2), and LogLM,
DeepTempo's encoder-only foundation model. LogLM leads at 0.95 verdict F1 at
under $0.0001 per alert; Claude Opus 4.7 is the best LLM at 0.93. The full
leaderboard, per-persona detail, and the F1/cost frontier are at
[socbench.org/detection/](https://socbench.org/detection/).

The repository is **local-first**. A laptop, three API keys, and a sample
parquet committed to the repo are enough to reproduce a smoke under a $10
budget. You can also run a complete smoke with **no API keys** via the mock
provider (see Quickstart step 3, or `notebooks/quickstart.ipynb`).

## Status

Alpha. The full pipeline runs end-to-end. Build-out covered:

- **Step 1**: package skeleton, contracts, configs, schema
- **Step 2**: the index builder (`socbench build-index`) with deterministic
  content-addressed indexes
- **Step 3**: read-only tools layer with persona allowlist + sample builder
- **Step 4**: personas, playbooks, prompt compose + forbidden-token check
- **Step 5**: provider adapters (OpenAI / Anthropic / Gemini / open-source
  endpoints + always-on mock) and the multi-turn agent loop with budget caps
  and cost/latency rollups
- **Step 6**: scoring (per-flow / per-pair / per-host F1), stratified
  sampling, ablation aggregation
- **Step 7**: quickstart + results-explorer notebooks; reproduction
  instructions in `REPRODUCE.md`

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

---

# PatchLoop: vulnerability detection and patching

[`src/socbench/patchloop`](src/socbench/patchloop) implements SOCBench's
second live capability. PatchLoop is a verification-loop remediation harness
for security findings: **propose a patch, apply it, verify it with the same
machinery that raised the alert, feed the verbatim failure back to the model,
and repair on top of the patched state.** Bounded attempts, typed errors,
fail-fast verification.

The headline result, on the original
[PatchEval](https://github.com/bytedance/PatchEval) benchmark (230 real-world
CVEs across Go, Python, and Node.js):

| Setup | Strict score | Note |
|---|---|---|
| No feedback | 77/230 = 33.5% | gpt-5-codex, direct-edit harness |
| One verify-feedback round | 132/230 = 57.4% | **+25.2pp from the loop alone**, ~$0.17/CVE |

That verify-feedback score clears general coding agents (OpenHands, Claude
Code, SWE-agent, all on GPT-5) on the original leaderboard by 20+ points,
while being roughly 10x cheaper per finding than an agent loop. The
feedback-round number was not eligible for that leaderboard (it uses
validation output); that is the point: in a SOC you have the detector that
raised the alert, and using it is free alpha.

> **Disclaimer.** These results are from the original PatchEval benchmark. On
> July 24, 2026, after these runs, ByteDance released **PatchEval-Verified**
> with revised Docker environments and a new leaderboard. The numbers above
> predate that release and are not comparable to PatchEval-Verified. A re-run
> against the updated version is planned.

The interactive walkthrough of the loop, the harness-vs-agent comparison, and
the full leaderboard are at
[socbench.org/patchloop/](https://socbench.org/patchloop/).

PatchLoop is a **standalone TypeScript project** (Node 20+, built on
[Effect](https://effect.website)). It is *not* installed by `pip install -e .`
and has no Python dependencies; it lives in this repo as the harness for this
capability. See its [README](src/socbench/patchloop/README.md) for design,
usage, and the verifier/router seams.

```bash
cd src/socbench/patchloop
npm install
npm test        # scripted patchers + marker verifiers, no LLM or scanners needed

# run the reference example against a real checkout:
export OPENROUTER_API_KEY=...
npx tsx examples/remediate-cve.ts --repo /path/to/checkout \
    --finding examples/finding-cve.json
```

---

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
- **New capability harness**: PatchLoop's `Router` registry is where new
  remediation harness families land (dependency bumps, IaC misconfigurations,
  detection-rule tuning); new SOC capabilities from the roadmap arrive as
  their own harnesses alongside detection.

## Methodology

The full detection methodology (eval units, persona × tool matrix, agent loop,
scoring, cost model, repair policy, sampling, ablations, run artifacts) is
implemented across the module-level files in `src/socbench/` (each carries a
focused module docstring). PatchLoop's loop rules are documented in its
[README](src/socbench/patchloop/README.md) and pinned by its test suite.
Higher-level explanations live at [socbench.org](https://socbench.org).

## License

Apache-2.0 for the repository; see [LICENSE](LICENSE). PatchLoop
(`src/socbench/patchloop`) is separately licensed under
[MIT](src/socbench/patchloop/LICENSE).

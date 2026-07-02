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

Alpha — the full pipeline runs end-to-end. Build-out follows the steps in
[`docs/implementation-plan.md`](docs/implementation-plan.md):

- **Step 1** — package skeleton, contracts, configs, schema
- **Step 2** — the index builder (`socbench build-index`) with deterministic
  content-addressed indexes
- **Step 3** — read-only tools layer with persona allowlist + sample builder
- **Step 4** — personas, playbooks, prompt compose + forbidden-token check
- **Step 5** — provider adapters (OpenAI / Anthropic / Gemini / open-source
  self-hosted + always-on mock) and the multi-turn agent loop with budget caps
  and cost/latency rollups
- **Step 6** — scoring (per-flow / per-pair / per-host F1), stratified
  sampling, ablation aggregation
- **Step 7** — quickstart + results-explorer notebooks, `RESULTS.md` /
  `RESULTS_REPRODUCE.md`

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
full `cost_budget_usd: 500`, fixed `cost_usd_cap_per_rendering: 0.50`. Paths
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
# Free, deterministic, no API keys — the mock provider:
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

## Running self-hosted / open-source models

`open_source_adapter.py` speaks plain OpenAI Chat Completions over HTTP
(`httpx`, no vendor SDK), so it works against **any** compatible server —
vLLM, Ollama, TGI, llama.cpp — local or remote. This is what was used to
benchmark self-hosted cybersecurity fine-tunes (e.g. Foundation-Sec-8B-Reasoning,
Seneca-Cybersecurity-LLM-x-QwQ-32B) alongside the hosted providers.

### Fastest path: point it at anything already running

```bash
export OPEN_SOURCE_BASE_URL=http://localhost:11434/v1   # Ollama's default
export OPEN_SOURCE_MODEL=<served-model-id>               # or set providers.open_source.model in config
socbench run --dataset-hash <dataset_hash> --providers open_source --personas all
```

No API key needed for a local server. For an authenticated remote endpoint
(e.g. a Vertex AI custom job), set `OPEN_SOURCE_API_KEY` to a bearer token.

### Adapter environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OPEN_SOURCE_BASE_URL` | `http://localhost:11434/v1` | Inference server base URL |
| `OPEN_SOURCE_MODEL` | — | Served model id; overrides `config/benchmark_config.yaml`'s `providers.open_source.model` (set by the deploy entrypoint to vLLM's `--served-model-name`) |
| `OPEN_SOURCE_API_KEY` | — | Optional bearer token (e.g. a short-lived GCP access token for a Vertex endpoint) |
| `OPEN_SOURCE_TIMEOUT_SECONDS` | `600` | Per-request read deadline. Self-hosted reasoning models under concurrent load take minutes per turn — raise this before assuming a model is broken |
| `OPEN_SOURCE_DISABLE_THINKING_BUDGET` | off | Stop sending vLLM's `thinking_token_budget` sampling param (some reasoner families/runners 400 on it — the adapter self-heals once automatically, this is the persistent override) |
| `OPEN_SOURCE_FLATTEN_TRANSCRIPT` | off | Collapse the multi-turn conversation into one role-labelled user message, for models whose chat template has no tool-role branch and mangles assistant history (e.g. Foundation-Sec-8B) |
| `OPEN_SOURCE_DEBUG_DUMP` | — | Path to a JSONL file; when set, every turn's raw response + how it was parsed is appended, for diagnosing a near-zero run |

Two CLI flags on `socbench run` are open-source-specific:

- `--explicit-tool-use-prompt` — swaps in an anti-narration system scaffold
  (`SYSTEM_SCAFFOLD_EXPLICIT` in `prompts.py`, documented in
  `config/prompts/system_scaffold_explicit.txt`) for models that narrate tool
  use in prose instead of calling tools. Off by default — capable models run
  on the same neutral scaffold the hosted providers use, so the comparison
  stays fair.
- `--context-window-tokens <N>` — the served `--max-model-len`. When set, the
  loop force-submits before the transcript would overflow it, instead of
  letting the server 400 the request (which the adapter would otherwise count
  as a lost, `adapter_fatal` rendering).

### Full GPU deployment (Vertex AI Custom Jobs)

`deploy/vertex/{launch.sh,entrypoint.sh,Dockerfile}` build and run a
containerized job that starts vLLM, health-checks it, pulls the dataset,
builds the index, runs `socbench run`, and uploads `runs/` to GCS —
end-to-end, no manual steps between "have a model id" and "have a scored run".
All knobs are environment variables so sibling launches stay consistent;
`launch.sh`'s header comment has full worked examples per model shape. The
essentials:

```bash
PROJECT=<gcp-project> TAG=<built-image-tag> \
MODEL=fdtn-ai/Foundation-Sec-8B-Reasoning \
FLATTEN_TRANSCRIPT=1 DISABLE_THINKING_BUDGET=1 \
OUTPUT_BUCKET=gs://<your-bucket>/os-benchmark/sec8 \
DISPLAY_NAME=sec8-full bash deploy/vertex/launch.sh
```

```bash
# A GGUF-only release (Seneca-x-QwQ-32B): dequantize to fp16 once and serve
# unquantized (vLLM's native path decodes 2-3x faster than its GGUF path).
PROJECT=<gcp-project> TAG=<built-image-tag> \
MODEL=AlicanKiraz0/Seneca-Cybersecurity-LLM-x-QwQ-32B-Q4_Medium-Version \
GGUF_FILENAME=senecallm-x-qwq-32b-q4_k_m.gguf TOKENIZER=Qwen/QwQ-32B \
DEQUANTIZE_GGUF=1 REASONING_PARSER=deepseek_r1 MAX_MODEL_LEN=16384 \
OUTPUT_BUCKET=gs://<your-bucket>/os-benchmark/seneca \
DISPLAY_NAME=seneca-full bash deploy/vertex/launch.sh
```

Key knobs beyond `MODEL`/`OUTPUT_BUCKET`/`DISPLAY_NAME`:

| Env var | Purpose |
|---|---|
| `MACHINE` / `ACCEL_TYPE` / `ACCEL_COUNT` / `TENSOR_PARALLEL` | GPU shape. Default `g2-standard-48` / `NVIDIA_L4` / 4 — see GPU notes below |
| `GGUF_FILENAME` / `TOKENIZER` / `DEQUANTIZE_GGUF` | GGUF-only model releases |
| `REASONING_PARSER` | vLLM `--reasoning-parser` (`deepseek_r1` for Qwen/QwQ-style `<think>` models; leave empty if no parser matches the model's CoT delimiter) |
| `FLATTEN_TRANSCRIPT` / `DISABLE_THINKING_BUDGET` / `EXPLICIT_TOOL_USE_PROMPT` | Shims for models whose template/tokenizer can't drive a native agentic tool-calling loop |
| `GPU_MEM_UTIL` | vLLM `--gpu-memory-utilization`. Lower to `0.80` on a 24GB L4 — the default (0.9) leaves too little free memory for the sampling warmup and OOMs at startup |
| `STRATEGY=FLEX_START MAX_WAIT=<dur>` | Dynamic Workload Scheduler — queues the job until GPU capacity frees up instead of failing immediately on "resources insufficient" |
| `LIMIT=<N>` | Run only the first N eval units — use for a cheap calibration slice before a full run |

**GPU availability notes (adjust for your project/quota):** L4 is the most
reliably available shape and works out of the box. A100 needs explicit quota.
H100 (`a3-highgpu-1g`) may ship a CUDA driver too old for current vLLM to
initialize — smoke-test with `LIMIT=10` before committing a full run to an
unfamiliar accelerator type.

### Diagnosing a near-zero run

`scripts/eval_sec8.py` decomposes a completed run into a behavioral funnel —
adapter-fatal rate, tool-invocation rate, voluntary-vs-forced submissions,
defect breakdown, evidence-grounding rate — so a near-zero score can be
attributed to a real capability limit vs. an infra/parsing problem before
it's reported as either:

```bash
python scripts/eval_sec8.py runs/<run_id> --provider open_source --persona soc_analyst --examples 3
# or machine-readable:
python scripts/eval_sec8.py runs/<run_id> --provider open_source --json
```

## Repository layout

```
socbench/
├── pyproject.toml
├── README.md
├── RESULTS.md                    # published-numbers skeleton
├── RESULTS_REPRODUCE.md          # exact commands behind each RESULTS row
├── LICENSE                       # Apache-2.0
├── config/                       # all YAML / JSON config in one place
│   ├── benchmark_config.yaml     # defaults, persona policy, providers, datasets
│   ├── pricing.yaml              # snapshot-dated provider pricing
│   ├── schema.json               # canonical NetFlow schema + aliases
│   └── prompts/                  # personas, playbooks, output contract (Step 4)
├── docs/
│   └── implementation-plan.md    # staged build-out roadmap
├── data/
│   └── sample/                   # ≤ 10 MB sample dataset
├── scripts/
│   └── build_sample_from_real.py # builds data/sample/ from a real source
├── notebooks/                    # quickstart, results explorer (Step 7)
├── src/socbench/                 # module-level files, not deep subpackages
│   ├── __init__.py / _version.py
│   ├── hashing.py                # deterministic content-addressed hashing
│   ├── logging_config.py
│   ├── config.py                 # typed loaders for benchmark_config / pricing
│   ├── schema.py                 # canonical record + alias + label inference
│   ├── models.py                 # pydantic v2 contracts (Flow, EvalUnit, ...)
│   ├── index.py                  # Step A: corpus index builder
│   ├── prompts.py                # Step 4: compose + forbidden-token check
│   ├── agent.py                  # Step 5: agent loop + Runner + summary rollups
│   ├── scoring.py                # Step 6: per-flow / per-pair / per-host F1
│   ├── sampling.py               # Step 6: stratified sampler
│   ├── aggregate.py              # Step 6: ablation joiner
│   ├── cli.py                    # `socbench` entrypoint
│   ├── providers/                # Step 5: one file per adapter
│   │   ├── base.py               #   Adapter ABC + request/response models + factory
│   │   ├── mock_adapter.py       #   deterministic, always-on, no SDK
│   │   ├── openai_adapter.py / anthropic_adapter.py / gemini_adapter.py
│   │   └── open_source_adapter.py #  any OpenAI-Chat-Completions-compatible server
│   └── tools/                    # Step 3: read-only tool layer
│       ├── base.py               #   Tool ABC + ToolContext + ToolRegistry
│       ├── smoke.py              #   diagnostic runner
│       └── catalog/              #   one file per shipped tool (+ build_default_registry)
└── tests/
```

## Extending the benchmark

Every interface designed to evolve is a registry or a YAML key:

- **New tool** — drop a new file under `src/socbench/tools/catalog/<name>.py`
  with a `Tool` subclass, register it in `src/socbench/tools/catalog/__init__.py`
  by appending to `ALL_TOOLS`, then add its name to the appropriate persona
  `tools:` lists in `config/benchmark_config.yaml`. The `tools_manifest_sha`
  shifts automatically. Filename ↔ YAML name ↔ matrix entry are 1:1 by design.
- **New eval-unit type** — add an assigner to `src/socbench/index.py` and a
  matching `Literal` to `EvalUnitType` in `src/socbench/models.py`.
- **New provider adapter** — implement the `Adapter` ABC in a new
  `src/socbench/providers/<name>_adapter.py`, register it in the
  `build_adapter` factory in `providers/base.py`, and add an entry under
  `providers:` in `config/benchmark_config.yaml`. Pricing goes in
  `config/pricing.yaml`. SDK imports stay lazy so the dependency is optional.
- **New persona** — add a block under `agent.personas:` in
  `config/benchmark_config.yaml` with its budget and `tools:` allowlist.
- **New scoring lens** — add a lens to `score_unit` in `src/socbench/scoring.py`
  and a matching field to `EvalUnitSummary` in `models.py`.
- **New ablation** — extend the `Ablation` handling in `prompts.py` / `agent.py`
  and the tag list in `aggregate.py`.

## Methodology

The full methodology — eval units, persona × tool matrix, agent loop, scoring,
cost model, repair policy, sampling, ablations, run artifacts — is implemented
across the module-level files in `src/socbench/` (each carries a focused module
docstring). The staged implementation roadmap is in
[`docs/implementation-plan.md`](docs/implementation-plan.md).

## License

Apache-2.0. See [LICENSE](LICENSE).

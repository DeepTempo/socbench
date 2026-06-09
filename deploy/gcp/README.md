# Running socbench on GCP (GKE Job)

Launch the full benchmark as a one-shot Kubernetes Job on the `tempo` GKE
cluster, then close your laptop. The Job pulls the dataset from GCS, builds the
index, runs the benchmark across every provider you have a key for, and uploads
the `runs/` artifact tree back to GCS.

## Prerequisites

- `gcloud` and `kubectl` authenticated to project `prod-loglm`.
- LLM API keys exported in your shell — only the ones you export are used:

  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  export OPENAI_API_KEY=sk-...
  export GEMINI_API_KEY=...        # or GOOGLE_API_KEY
  ```

## Launch

```bash
./deploy/gcp/launch.sh
```

That builds + pushes the image (Cloud Build, `linux/amd64`), upserts the
`socbench-llm-keys` secret from your env, detects available providers, and
applies the Job. Then follow it:

```bash
kubectl logs -f job/socbench-full-<tag> -n tempo
kubectl get  job/socbench-full-<tag> -n tempo -w
```

## Configuration (env overrides)

| Var | Default | Meaning |
|---|---|---|
| `MODE` | `full` | `smoke` or `full` |
| `COST_BUDGET_USD` | `500` | run aborts cleanly when reached |
| `PROVIDERS` | auto-detected from keys | CSV like `openai,anthropic,gemini` |
| `DATASET_GCS` | `gs://tempo-datasets-001/benchmark-v0/combined/benchmark-v0-canonical.parquet` | canonical parquet |
| `RESULTS_GCS` | `gs://tempo-datasets-001/benchmark-v0/results` | upload prefix |
| `TAG` | git short SHA | image tag |
| `PROJECT` / `REGION` / `NAMESPACE` | `prod-loglm` / `us-west1` / `tempo` | cluster coords |

Example — only Anthropic, tighter budget:

```bash
PROVIDERS=anthropic COST_BUDGET_USD=150 ./deploy/gcp/launch.sh
```

## Results

Each run uploads to `${RESULTS_GCS}/runs/<run_id>/`. Pull and inspect:

```bash
gcloud storage cp -r \
  gs://tempo-datasets-001/benchmark-v0/results/runs ./runs-from-gcp
python -m json.tool runs-from-gcp/<run_id>/summary.json
```

## How auth works in-cluster

The Job runs as `tempo-sa`, whose workload identity grants GCS access — so the
dataset pull and results upload need no extra credentials. Only the LLM provider
keys come from the `socbench-llm-keys` secret (mounted as optional env vars, so
missing providers are simply skipped).

## Notes

- Image is built with Cloud Build so it's `linux/amd64` regardless of your
  laptop's architecture.
- `activeDeadlineSeconds: 28800` (8h) caps a stuck run; raise it in `job.yaml`
  if a full three-provider sweep needs longer.
- The Job is kept for 48h after finishing (`ttlSecondsAfterFinished`) so you can
  still read its logs.

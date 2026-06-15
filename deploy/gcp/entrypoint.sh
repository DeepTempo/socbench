#!/usr/bin/env bash
# socbench batch entrypoint (runs inside the GKE Job).
#
# 1. pull the canonical dataset parquet from GCS
# 2. build the content-addressed index
# 3. run the benchmark (full mode, all providers that have a key)
# 4. upload the runs/ artifact tree back to GCS
#
# Configured entirely via env vars (see deploy/gcp/job.yaml):
#   DATASET_GCS       gs:// URI of the canonical parquet (required)
#   RESULTS_GCS       gs:// prefix to upload runs/ under (required)
#   PROVIDERS         CSV like "openai,anthropic,gemini" (required)
#   MODE              smoke|full (default: full)
#   PERSONAS          "all" or CSV (default: all)
#   COST_BUDGET_USD   abort cleanly when reached (default: 500)
#   DATASET_LOCAL     local staging path (default: data/benchmark-v0.parquet)
set -euo pipefail

: "${DATASET_GCS:?set DATASET_GCS}"
: "${RESULTS_GCS:?set RESULTS_GCS}"
: "${PROVIDERS:?set PROVIDERS}"
MODE="${MODE:-full}"
PERSONAS="${PERSONAS:-all}"
COST_BUDGET_USD="${COST_BUDGET_USD:-500}"
DATASET_LOCAL="${DATASET_LOCAL:-data/benchmark-v0.parquet}"

echo "[entrypoint] providers=${PROVIDERS} mode=${MODE} budget=${COST_BUDGET_USD}"

echo "[entrypoint] pulling dataset ${DATASET_GCS} -> ${DATASET_LOCAL}"
DATASET_GCS="$DATASET_GCS" DATASET_LOCAL="$DATASET_LOCAL" python - <<'PY'
import os, gcsfs
fs = gcsfs.GCSFileSystem()
src = os.environ["DATASET_GCS"].removeprefix("gs://")
dst = os.environ["DATASET_LOCAL"]
os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
fs.get(src, dst)
print(f"[entrypoint] pulled {fs.info(src)['size']:,} bytes")
PY

echo "[entrypoint] building index"
BUILD_OUT="$(socbench --log-level WARNING build-index --dataset benchmark-v0)"
echo "$BUILD_OUT"
DATASET_HASH="$(printf '%s\n' "$BUILD_OUT" | sed -n 's/.*dataset_hash=\([a-f0-9][a-f0-9]*\).*/\1/p' | head -1)"
if [ -z "$DATASET_HASH" ]; then
    echo "[entrypoint] FATAL: could not parse dataset_hash from build-index output" >&2
    exit 1
fi
echo "[entrypoint] dataset_hash=${DATASET_HASH}"

# Upload whatever is currently in runs/ to GCS. Safe to call repeatedly; the
# per-unit jsonl artifacts are written incrementally by socbench, so each call
# checkpoints the latest completed renderings.
upload_runs() {
    RESULTS_GCS="$RESULTS_GCS" python - <<'PY'
import os, gcsfs
fs = gcsfs.GCSFileSystem()
dst = os.environ["RESULTS_GCS"].removeprefix("gs://").rstrip("/")
if os.path.isdir("runs"):
    fs.put("runs", f"{dst}/runs", recursive=True)
    print(f"[entrypoint] uploaded runs/ -> gs://{dst}/runs", flush=True)
else:
    print("[entrypoint] no runs/ directory to upload yet", flush=True)
PY
}

# Periodic checkpoint loop: pushes partial runs/ to GCS so a task-timeout kill
# (Cloud Run has no exec into running tasks) never loses completed work.
UPLOAD_INTERVAL_SEC="${UPLOAD_INTERVAL_SEC:-600}"
(
    while true; do
        sleep "$UPLOAD_INTERVAL_SEC"
        echo "[entrypoint] periodic checkpoint upload" >&2
        upload_runs || echo "[entrypoint] WARN periodic upload failed" >&2
    done
) &
CHECKPOINT_PID=$!
trap 'kill "$CHECKPOINT_PID" 2>/dev/null || true' EXIT

echo "[entrypoint] running benchmark (checkpoint every ${UPLOAD_INTERVAL_SEC}s)"
set +e
socbench run \
    --dataset-hash "$DATASET_HASH" \
    --providers "$PROVIDERS" \
    --personas "$PERSONAS" \
    --mode "$MODE" \
    --cost-budget-usd "$COST_BUDGET_USD"
RUN_RC=$?
set -e
echo "[entrypoint] run exited rc=${RUN_RC}"

kill "$CHECKPOINT_PID" 2>/dev/null || true

# Final authoritative upload (captures the parquet mirrors + summary.json that
# socbench only writes at the very end).
echo "[entrypoint] final upload runs/ -> ${RESULTS_GCS}"
upload_runs

echo "[entrypoint] done"
exit "$RUN_RC"

#!/usr/bin/env bash
# socbench Vertex AI Custom Job entrypoint.
#
# 1. Parses container args (--model, --output-bucket, etc.)
# 2. Starts vLLM OpenAI-compatible server in the background  [skipped in --dry-run]
# 3. Polls /health until the model is ready                  [skipped in --dry-run]
# 4. Pulls dataset from GCS + builds index
# 5. Runs socbench against localhost:8000                    [skipped in --dry-run]
# 6. Uploads runs/ to GCS                                   [skipped in --dry-run]
#
set -euo pipefail

# ---------- defaults ----------
MODEL=""
QUANTIZATION=""
MAX_MODEL_LEN="8192"
DTYPE="auto"
TENSOR_PARALLEL="1"
TOOL_CALL_PARSER="hermes"
TOKENIZER=""
GGUF_FILENAME=""
DATASET_GCS=""
OUTPUT_BUCKET=""
MODE="full"
PERSONAS="all"
COST_BUDGET_USD="0"
LIMIT=""
DRY_RUN="0"

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)             MODEL="$2";             shift 2 ;;
        --quantization)      QUANTIZATION="$2";      shift 2 ;;
        --max-model-len)     MAX_MODEL_LEN="$2";     shift 2 ;;
        --dtype)             DTYPE="$2";             shift 2 ;;
        --tensor-parallel)   TENSOR_PARALLEL="$2";   shift 2 ;;
        --tool-call-parser)  TOOL_CALL_PARSER="$2";  shift 2 ;;
        --tokenizer)         TOKENIZER="$2";         shift 2 ;;
        --gguf-filename)     GGUF_FILENAME="$2";     shift 2 ;;
        --dataset-gcs)       DATASET_GCS="$2";       shift 2 ;;
        --output-bucket)     OUTPUT_BUCKET="$2";     shift 2 ;;
        --mode)              MODE="$2";              shift 2 ;;
        --personas)          PERSONAS="$2";          shift 2 ;;
        --cost-budget-usd)   COST_BUDGET_USD="$2";   shift 2 ;;
        --limit)             LIMIT="$2";             shift 2 ;;
        --dry-run)           DRY_RUN="1";            shift ;;
        --model=*)             MODEL="${1#*=}";             shift ;;
        --quantization=*)      QUANTIZATION="${1#*=}";      shift ;;
        --max-model-len=*)     MAX_MODEL_LEN="${1#*=}";     shift ;;
        --dtype=*)             DTYPE="${1#*=}";             shift ;;
        --tensor-parallel=*)   TENSOR_PARALLEL="${1#*=}";   shift ;;
        --tool-call-parser=*)  TOOL_CALL_PARSER="${1#*=}";  shift ;;
        --tokenizer=*)         TOKENIZER="${1#*=}";         shift ;;
        --gguf-filename=*)     GGUF_FILENAME="${1#*=}";     shift ;;
        --dataset-gcs=*)       DATASET_GCS="${1#*=}";       shift ;;
        --output-bucket=*)     OUTPUT_BUCKET="${1#*=}";     shift ;;
        --mode=*)              MODE="${1#*=}";              shift ;;
        --personas=*)          PERSONAS="${1#*=}";          shift ;;
        --cost-budget-usd=*)   COST_BUDGET_USD="${1#*=}";   shift ;;
        --limit=*)             LIMIT="${1#*=}";             shift ;;
        *) echo "[entrypoint] WARN: unknown arg $1"; shift ;;
    esac
done

if [[ -z "$MODEL" ]]; then
    echo "[entrypoint] FATAL: --model is required" >&2
    exit 1
fi

echo "[entrypoint] model=${MODEL} mode=${MODE} personas=${PERSONAS} dry_run=${DRY_RUN}"

# ---------- GGUF pre-download (skipped in dry-run) ----------
# vLLM v0.22 cannot resolve hf:// URIs or GGUF-only repos (no config.json).
# Download the file first so vLLM gets a plain local path.
if [[ -n "$GGUF_FILENAME" && "$DRY_RUN" != "1" ]]; then
    echo "[entrypoint] downloading GGUF ${MODEL}/${GGUF_FILENAME} ..."
    python3 -c "
from huggingface_hub import hf_hub_download
path = hf_hub_download(
    repo_id='${MODEL}',
    filename='${GGUF_FILENAME}',
    local_dir='/tmp/gguf-model',
)
print(f'[entrypoint] downloaded -> {path}')
"
    MODEL="/tmp/gguf-model/${GGUF_FILENAME}"
    echo "[entrypoint] model path: ${MODEL}"
fi

# ---------- vLLM (skipped in dry-run) ----------
VLLM_PID=""
# Tear down vLLM and ALL its children (EngineCore, TP workers). vLLM is launched
# under `setsid`, so VLLM_PID is its process-group leader and `kill -- -PID`
# signals the whole group. We deliberately avoid a bare `wait`: a process stuck
# mid-compile can ignore SIGTERM, and `wait` would then block forever — that is
# exactly what left jobs in JOB_STATE_RUNNING for days, holding a GPU. Instead we
# SIGTERM, poll for up to 20s, then SIGKILL, so the container is guaranteed to exit.
cleanup() {
    [[ -z "$VLLM_PID" ]] && return 0
    echo "[entrypoint] shutting down vLLM (pgid=${VLLM_PID})"
    kill -TERM "-${VLLM_PID}" 2>/dev/null || kill -TERM "${VLLM_PID}" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "${VLLM_PID}" 2>/dev/null || return 0
        sleep 1
    done
    echo "[entrypoint] vLLM ignored SIGTERM; sending SIGKILL"
    kill -KILL "-${VLLM_PID}" 2>/dev/null || kill -KILL "${VLLM_PID}" 2>/dev/null || true
    return 0
}
trap cleanup EXIT

if [[ "$DRY_RUN" != "1" ]]; then
    VLLM_ARGS=(
        --model "$MODEL"
        --max-model-len "$MAX_MODEL_LEN"
        --dtype "$DTYPE"
        --tensor-parallel-size "$TENSOR_PARALLEL"
        --port 8000
        --host 0.0.0.0
        --enable-auto-tool-choice
        --tool-call-parser "$TOOL_CALL_PARSER"
    )
    [[ -n "$QUANTIZATION" ]] && VLLM_ARGS+=(--quantization "$QUANTIZATION")
    [[ -n "$TOKENIZER" ]] && VLLM_ARGS+=(--tokenizer "$TOKENIZER")

    echo "[entrypoint] starting vLLM server..."
    # setsid: run vLLM in its own process group so cleanup() can kill the whole
    # tree (EngineCore + TP workers), not just the parent PID.
    setsid python3 -m vllm.entrypoints.openai.api_server "${VLLM_ARGS[@]}" &
    VLLM_PID=$!

    # Loading weights + torch.compile + CUDA-graph capture can take many minutes
    # for large / GGUF / tensor-parallel models (e.g. seneca-32b's engine init
    # alone took ~260s). The old 240s cap fired mid-startup, then the job hung on
    # shutdown and burned a GPU for days. Default 30 min; override with
    # VLLM_HEALTH_TIMEOUT_SEC. Bail immediately if vLLM dies so we fail fast.
    HEALTH_TIMEOUT_SEC="${VLLM_HEALTH_TIMEOUT_SEC:-1800}"
    echo "[entrypoint] waiting up to ${HEALTH_TIMEOUT_SEC}s for vLLM /health ..."
    HEALTHY=0
    SECS=0
    while (( SECS < HEALTH_TIMEOUT_SEC )); do
        if ! kill -0 "$VLLM_PID" 2>/dev/null; then
            echo "[entrypoint] FATAL: vLLM server process exited during startup" >&2
            exit 1
        fi
        if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
            HEALTHY=1
            break
        fi
        sleep 5
        SECS=$(( SECS + 5 ))
    done
    if [[ "$HEALTHY" != "1" ]]; then
        echo "[entrypoint] FATAL: vLLM did not become healthy within ${HEALTH_TIMEOUT_SEC}s" >&2
        exit 1
    fi
    echo "[entrypoint] vLLM is ready after ~${SECS}s"
fi

# ---------- dataset ----------
DATASET_LOCAL="data/benchmark-v0.parquet"
DATASET_NAME="benchmark-v0"

if [[ -n "$DATASET_GCS" ]]; then
    echo "[entrypoint] pulling dataset ${DATASET_GCS} -> ${DATASET_LOCAL}"
    python3 -c "
import gcsfs, os
fs = gcsfs.GCSFileSystem()
src = '${DATASET_GCS}'.removeprefix('gs://')
dst = '${DATASET_LOCAL}'
os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
fs.get(src, dst)
info = fs.info(src)
print(f'[entrypoint] pulled {info[\"size\"]:,} bytes')
"
else
    echo "[entrypoint] no --dataset-gcs; using built-in sample"
    DATASET_LOCAL="data/sample/cic2018-mini.parquet"
    DATASET_NAME="sample"
fi

# ---------- build index ----------
echo "[entrypoint] building index for ${DATASET_NAME}"
BUILD_OUT="$(socbench --log-level WARNING build-index --dataset "${DATASET_NAME}" 2>&1)"
echo "$BUILD_OUT"
DATASET_HASH="$(printf '%s\n' "$BUILD_OUT" | sed -n 's/.*dataset_hash=\([a-f0-9][a-f0-9]*\).*/\1/p' | head -1)"
if [[ -z "$DATASET_HASH" ]]; then
    echo "[entrypoint] FATAL: could not parse dataset_hash from build-index output" >&2
    exit 1
fi
echo "[entrypoint] dataset_hash=${DATASET_HASH}"

# ---------- dry-run exits here ----------
if [[ "$DRY_RUN" == "1" ]]; then
    echo "[entrypoint] dry-run complete — GCS access OK, index built, dataset_hash=${DATASET_HASH}"
    exit 0
fi

# ---------- run benchmark ----------
export OPEN_SOURCE_BASE_URL="http://localhost:8000/v1"
export OPEN_SOURCE_MODEL="$MODEL"
export OPEN_SOURCE_API_KEY=""

echo "[entrypoint] running benchmark against open_source (localhost:8000)"
set +e
BENCH_ARGS=(
    run
    --dataset-hash "$DATASET_HASH"
    --providers open_source
    --personas "$PERSONAS"
    --mode "$MODE"
)
[[ "$COST_BUDGET_USD" != "0" ]] && BENCH_ARGS+=(--cost-budget-usd "$COST_BUDGET_USD")
# --limit N runs the first N eval units (sorted id) — used for cheap calibration slices.
[[ -n "$LIMIT" ]] && BENCH_ARGS+=(--limit "$LIMIT")

# Wall-clock backstop: a hung run must never burn the GPU indefinitely (Vertex's
# default job timeout is 7 days). Default 24h; override with RUN_TIMEOUT.
RUN_TIMEOUT="${RUN_TIMEOUT:-24h}"
if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=120 "$RUN_TIMEOUT" socbench "${BENCH_ARGS[@]}"
else
    socbench "${BENCH_ARGS[@]}"
fi
RUN_RC=$?
set -e
if [[ "$RUN_RC" == "124" ]]; then
    echo "[entrypoint] WARN: benchmark exceeded RUN_TIMEOUT=${RUN_TIMEOUT}; uploading partial results" >&2
fi
echo "[entrypoint] run exited rc=${RUN_RC}"

# ---------- upload results ----------
# Loud, explicit branches: a silently-skipped upload previously looked identical
# to a successful one, sending people hunting an empty bucket.
UPLOAD_RC=0
if [[ -z "$OUTPUT_BUCKET" ]]; then
    echo "[entrypoint] WARN: --output-bucket not set; results stay in-container and are LOST on exit" >&2
elif [[ ! -d "runs" ]]; then
    echo "[entrypoint] WARN: no runs/ directory produced (run likely failed before writing artifacts); nothing to upload" >&2
else
    echo "[entrypoint] uploading runs/ -> ${OUTPUT_BUCKET%/}/runs"
    set +e
    python3 -c "
import gcsfs
fs = gcsfs.GCSFileSystem()
dst = '${OUTPUT_BUCKET}'.removeprefix('gs://').rstrip('/')
fs.put('runs', f'{dst}/runs', recursive=True)
print(f'[entrypoint] uploaded runs/ -> gs://{dst}/runs')
"
    UPLOAD_RC=$?
    set -e
    [[ "$UPLOAD_RC" != "0" ]] && echo "[entrypoint] WARN: upload failed (rc=${UPLOAD_RC}); results remain only in-container" >&2
fi

echo "[entrypoint] done (run_rc=${RUN_RC} upload_rc=${UPLOAD_RC})"
# Surface a run failure first; otherwise surface an upload failure.
[[ "$RUN_RC" != "0" ]] && exit "$RUN_RC"
exit "$UPLOAD_RC"

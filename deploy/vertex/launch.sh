#!/usr/bin/env bash
# Launch a socbench benchmark as a Vertex AI Custom Job (local vLLM inference).
#
# Why this exists: the original jobs were submitted with a hand-written
# `gcloud ai custom-jobs create` and NO --timeout, so a wedged container sat in
# JOB_STATE_RUNNING for days holding GPUs (Vertex's default job timeout is 7d).
# This launcher ALWAYS sets --timeout (default 8h) as a hard backstop, on top of
# the entrypoint's own startup/run guards.
#
# All knobs are env vars so sibling launches stay consistent. Examples:
#
#   # sec-8b full run on 4x L4
#   MODEL=fdtn-ai/Foundation-Sec-8B-Reasoning \
#   OUTPUT_BUCKET=gs://your-bucket/os-benchmark/sec-8b \
#   DISPLAY_NAME=sec-8b-full bash deploy/vertex/launch.sh
#
#   # seneca-32b GGUF full run on 4x L4
#   MODEL=AlicanKiraz0/Seneca-Cybersecurity-LLM-x-QwQ-32B-Q4_Medium-Version \
#   GGUF_FILENAME=senecallm-x-qwq-32b-q4_k_m.gguf TOKENIZER=Qwen/QwQ-32B \
#   QUANTIZATION=gguf OUTPUT_BUCKET=gs://your-bucket/os-benchmark/seneca-32b \
#   DISPLAY_NAME=seneca-32b-full bash deploy/vertex/launch.sh
#
#   # cheap calibration slice (first 40 units) — add LIMIT + a short timeout
#   LIMIT=40 JOB_TIMEOUT=3600s DISPLAY_NAME=sec-8b-calib ... bash deploy/vertex/launch.sh
set -euo pipefail

PROJECT="${PROJECT:-your-gcp-project}"
REGION="${REGION:-us-central1}"
REGISTRY="${REGISTRY:-us-central1-docker.pkg.dev/${PROJECT}/socbench}"
TAG="${TAG:?set TAG to the built image tag, e.g. aca086e-fix10}"
IMAGE="${IMAGE:-${REGISTRY}/socbench-vertex:${TAG}}"

# --- hardware (4x L4 by default — no A100s) ----------------------------------
MACHINE="${MACHINE:-g2-standard-48}"
ACCEL_TYPE="${ACCEL_TYPE:-NVIDIA_L4}"
ACCEL_COUNT="${ACCEL_COUNT:-4}"
TENSOR_PARALLEL="${TENSOR_PARALLEL:-${ACCEL_COUNT}}"   # default TP == #GPUs

# --- model / run -------------------------------------------------------------
MODEL="${MODEL:?set MODEL (HF repo id)}"
GGUF_FILENAME="${GGUF_FILENAME:-}"
TOKENIZER="${TOKENIZER:-}"
QUANTIZATION="${QUANTIZATION:-}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-hermes}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
DATASET_GCS="${DATASET_GCS:-gs://your-bucket/os-benchmark/data/benchmark-v0.parquet}"
OUTPUT_BUCKET="${OUTPUT_BUCKET:?set OUTPUT_BUCKET (gs:// prefix; runs/ is appended)}"
MODE="${MODE:-full}"
PERSONAS="${PERSONAS:-all}"
LIMIT="${LIMIT:-}"
COST_BUDGET_USD="${COST_BUDGET_USD:-0}"

# --- the safety backstop: hard job timeout (default 8h) ----------------------
JOB_TIMEOUT="${JOB_TIMEOUT:-28800s}"
DISPLAY_NAME="${DISPLAY_NAME:?set DISPLAY_NAME}"

# --- assemble container args -------------------------------------------------
declare -a ARGS=( "--model=${MODEL}" "--tensor-parallel=${TENSOR_PARALLEL}"
                  "--max-model-len=${MAX_MODEL_LEN}" "--dataset-gcs=${DATASET_GCS}"
                  "--output-bucket=${OUTPUT_BUCKET}" "--mode=${MODE}"
                  "--personas=${PERSONAS}" "--tool-call-parser=${TOOL_CALL_PARSER}" )
[[ -n "$GGUF_FILENAME"   ]] && ARGS+=( "--gguf-filename=${GGUF_FILENAME}" )
[[ -n "$TOKENIZER"       ]] && ARGS+=( "--tokenizer=${TOKENIZER}" )
[[ -n "$QUANTIZATION"    ]] && ARGS+=( "--quantization=${QUANTIZATION}" )
[[ -n "$LIMIT"           ]] && ARGS+=( "--limit=${LIMIT}" )
[[ "$COST_BUDGET_USD" != "0" ]] && ARGS+=( "--cost-budget-usd=${COST_BUDGET_USD}" )

BOOT_DISK_GB="${BOOT_DISK_GB:-200}"

# custom-jobs create has no --timeout flag; scheduling.timeout must come via a
# job-spec YAML (which also carries the worker pool spec + container args).
SPEC_FILE="$(mktemp -t socbench-vertex-XXXX.yaml)"
trap 'rm -f "$SPEC_FILE"' EXIT
{
    echo "workerPoolSpecs:"
    echo "  - replicaCount: 1"
    echo "    machineSpec:"
    echo "      machineType: ${MACHINE}"
    echo "      acceleratorType: ${ACCEL_TYPE}"
    echo "      acceleratorCount: ${ACCEL_COUNT}"
    echo "    diskSpec:"
    echo "      bootDiskType: pd-ssd"
    echo "      bootDiskSizeGb: ${BOOT_DISK_GB}"
    echo "    containerSpec:"
    echo "      imageUri: ${IMAGE}"
    echo "      args:"
    for a in "${ARGS[@]}"; do echo "        - \"${a}\""; done
    echo "scheduling:"
    echo "  timeout: ${JOB_TIMEOUT}"
} > "$SPEC_FILE"

echo "=== Vertex Custom Job: ${DISPLAY_NAME} ==="
echo "  project=${PROJECT} region=${REGION}"
echo "  image=${IMAGE}"
echo "  hw=${MACHINE} ${ACCEL_COUNT}x${ACCEL_TYPE} tp=${TENSOR_PARALLEL} disk=${BOOT_DISK_GB}Gb timeout=${JOB_TIMEOUT}"
echo "  model=${MODEL} mode=${MODE} limit=${LIMIT:-<none>} -> ${OUTPUT_BUCKET}/runs"
echo "--- job spec ---"; cat "$SPEC_FILE"; echo "----------------"

gcloud ai custom-jobs create \
    --project="${PROJECT}" \
    --region="${REGION}" \
    --display-name="${DISPLAY_NAME}" \
    --config="${SPEC_FILE}"

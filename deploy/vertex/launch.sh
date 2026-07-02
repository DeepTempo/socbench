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
#   # sec-8b full run on 4x L4. Its chat template can't drive an agent loop and
#   # no vLLM tool/reasoning parser matches its output, so DON'T set a reasoning
#   # parser; instead flatten the transcript (so it sees tool results + its own
#   # calls) and disable the thinking-budget param. Tool calls are recovered from
#   # content by the adapter fallback.
#   MODEL=fdtn-ai/Foundation-Sec-8B-Reasoning \
#   FLATTEN_TRANSCRIPT=1 DISABLE_THINKING_BUDGET=1 \
#   OUTPUT_BUCKET=gs://your-bucket/os-benchmark/sec-8b \
#   DISPLAY_NAME=sec-8b-full bash deploy/vertex/launch.sh
#
#   # seneca-32b full run on 1x A100-80GB — RECOMMENDED. The finetune is GGUF-only,
#   # so dequantize to fp16 once and serve unquantized (fast native kernels). Qwen/QwQ
#   # has a proper tool template + <think> reasoning -> native deepseek_r1 + hermes,
#   # no flatten. MUST use the 80GB A100 (a2-ultragpu-1g / NVIDIA_A100_80GB): the
#   # 40GB a2-highgpu A100 cannot hold the ~64GB fp16 weights and will OOM at load.
#   # TENSOR_PARALLEL=1 (single GPU). GPU_MEM_UTIL=0.95 frees KV-cache room for the
#   # tight fp16 fit. thinking_token_budget self-heals if QwQ's runtime rejects it.
#   MODEL=AlicanKiraz0/Seneca-Cybersecurity-LLM-x-QwQ-32B-Q4_Medium-Version \
#   GGUF_FILENAME=senecallm-x-qwq-32b-q4_k_m.gguf TOKENIZER=Qwen/QwQ-32B \
#   DEQUANTIZE_GGUF=1 REASONING_PARSER=deepseek_r1 MAX_MODEL_LEN=16384 \
#   MACHINE=a2-ultragpu-1g ACCEL_TYPE=NVIDIA_A100_80GB ACCEL_COUNT=1 \
#   TENSOR_PARALLEL=1 GPU_MEM_UTIL=0.95 JOB_TIMEOUT=86400s \
#   OUTPUT_BUCKET=gs://your-bucket/os-benchmark/seneca-32b \
#   DISPLAY_NAME=seneca-32b-full bash deploy/vertex/launch.sh
#
#   # Validate the WHOLE pipeline cheaply first (download+dequant+serve+score on a
#   # few units) before the real run — add LIMIT and a short timeout, one persona:
#   #   LIMIT=20 PERSONAS=soc_analyst JOB_TIMEOUT=10800s DISPLAY_NAME=seneca-smoke ...
#
#   # seneca-32b on 4x L4 (fp16) — slower alternative if no A100 quota.
#   MODEL=AlicanKiraz0/Seneca-Cybersecurity-LLM-x-QwQ-32B-Q4_Medium-Version \
#   GGUF_FILENAME=senecallm-x-qwq-32b-q4_k_m.gguf TOKENIZER=Qwen/QwQ-32B \
#   DEQUANTIZE_GGUF=1 REASONING_PARSER=deepseek_r1 MAX_MODEL_LEN=16384 \
#   JOB_TIMEOUT=43200s OUTPUT_BUCKET=gs://your-bucket/os-benchmark/seneca-32b \
#   DISPLAY_NAME=seneca-32b-full bash deploy/vertex/launch.sh
#
#   # seneca-32b SLOW path (no conversion): serve the GGUF directly. Simpler, but
#   # decode is much slower — only viable for a single persona / small slice.
#   MODEL=AlicanKiraz0/Seneca-Cybersecurity-LLM-x-QwQ-32B-Q4_Medium-Version \
#   GGUF_FILENAME=senecallm-x-qwq-32b-q4_k_m.gguf TOKENIZER=Qwen/QwQ-32B \
#   QUANTIZATION=gguf REASONING_PARSER=deepseek_r1 \
#   OUTPUT_BUCKET=gs://your-bucket/os-benchmark/seneca-32b \
#   DISPLAY_NAME=seneca-32b-slow bash deploy/vertex/launch.sh
#
#   # cheap calibration slice (first 40 units) — add LIMIT + a short timeout.
#   # 3600s is fine for the 8B; a 32B GGUF needs longer (download + load can eat
#   # ~15-20 min before the first request), so give seneca >=7200s or it gets
#   # cancelled mid-load with zero output (socbench only uploads at the end).
#   LIMIT=40 JOB_TIMEOUT=3600s DISPLAY_NAME=sec-8b-calib ... bash deploy/vertex/launch.sh
#   LIMIT=40 JOB_TIMEOUT=7200s DISPLAY_NAME=seneca-32b-calib ... bash deploy/vertex/launch.sh
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
# Reasoning-CoT separator (vLLM --reasoning-parser). Set ONLY when the model's
# delimiter matches a parser:
#   QwQ-32B / Seneca (Qwen 2.5) -> deepseek_r1  (matches <think>…</think>)
#   Foundation-Sec-8B           -> leave EMPTY (no matching parser; CoT stays in
#       content and the adapter fallback recovers the call). Empty -> omitted.
REASONING_PARSER="${REASONING_PARSER:-}"
# Foundation-Sec-8B shims (default off — Qwen/QwQ/Seneca need neither):
#   FLATTEN_TRANSCRIPT=1      its template has no tool-role branch and mangles
#                            assistant history, so deliver the whole transcript
#                            as one user message (else it never sees tool
#                            results or its own prior calls -> loops -> 0).
#   DISABLE_THINKING_BUDGET=1 it matches no reasoning parser, so don't send the
#                            vLLM thinking_token_budget param (would 400 the run);
#                            max_tokens is still widened for the inline CoT.
FLATTEN_TRANSCRIPT="${FLATTEN_TRANSCRIPT:-0}"
DISABLE_THINKING_BUDGET="${DISABLE_THINKING_BUDGET:-0}"
# EXPLICIT_TOOL_USE_PROMPT=1 selects the anti-narration scaffold (sec8 shim). Leave
# 0 for capable models (Seneca, GLM, …) so they run on the neutral, fair scaffold.
EXPLICIT_TOOL_USE_PROMPT="${EXPLICIT_TOOL_USE_PROMPT:-0}"
# Large-MoE serving knobs (GLM-5.2 W4A16): expert parallelism, fp8 KV-cache, MTP
# speculative decoding, trust-remote-code. Default off/empty (dense models ignore).
ENABLE_EXPERT_PARALLEL="${ENABLE_EXPERT_PARALLEL:-0}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-}"
MTP_NUM_TOKENS="${MTP_NUM_TOKENS:-}"
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-0}"
# SPOT=1 runs on preemptible Spot GPUs — much cheaper, but Vertex can reclaim the
# node mid-run, and socbench only uploads artifacts at the END, so a preemption
# loses the whole run. Use for short slices, or accept the restart risk on long ones.
SPOT="${SPOT:-0}"
# DEQUANTIZE_GGUF=1: convert a GGUF-only release to fp16 safetensors and serve it
# unquantized (fast native vLLM path, ~2-3x the GGUF decode speed). Requires
# GGUF_FILENAME. Use for Seneca-x-QwQ-32B to fit the 1500-unit run in budget.
DEQUANTIZE_GGUF="${DEQUANTIZE_GGUF:-0}"
# GPU memory fraction for vLLM. Leave empty for the default; set 0.95 when fitting
# a 32B fp16 model on a single 80GB A100 so KV cache has room to admit a sequence.
GPU_MEM_UTIL="${GPU_MEM_UTIL:-}"
# socbench run wall-clock backstop (passed to entrypoint). Empty -> entrypoint
# default (24h). Set 47h for an all-personas 32B run that exceeds a day, and keep
# JOB_TIMEOUT above it so Vertex doesn't kill the job before this clean cutoff.
RUN_TIMEOUT="${RUN_TIMEOUT:-}"
# Total sequence budget (prompt + reasoning + output). Reasoning models need
# headroom for an 8k-token thinking budget on top of the prompt and output, so
# default high; Foundation-Sec's own card recommends 32768. Lower it only for
# non-reasoning models or tight GPU memory.
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
DATASET_GCS="${DATASET_GCS:-gs://your-bucket/os-benchmark/data/benchmark-v0.parquet}"
OUTPUT_BUCKET="${OUTPUT_BUCKET:?set OUTPUT_BUCKET (gs:// prefix; runs/ is appended)}"
MODE="${MODE:-full}"
PERSONAS="${PERSONAS:-all}"
LIMIT="${LIMIT:-}"
COST_BUDGET_USD="${COST_BUDGET_USD:-0}"

# --- the safety backstop: hard job timeout (default 8h) ----------------------
JOB_TIMEOUT="${JOB_TIMEOUT:-28800s}"
DISPLAY_NAME="${DISPLAY_NAME:?set DISPLAY_NAME}"

# Provisioning strategy. SPOT=1 (legacy) maps to STRATEGY=SPOT. STRATEGY=FLEX_START
# is Dynamic Workload Scheduler: the job QUEUES until GPUs free up (up to MAX_WAIT)
# instead of failing immediately with "resources insufficient" — the right tool
# when on-demand capacity is exhausted. MAX_WAIT is the queue wait window.
STRATEGY="${STRATEGY:-}"
[[ "$SPOT" == "1" ]] && STRATEGY="SPOT"
MAX_WAIT="${MAX_WAIT:-86400s}"

# --- assemble container args -------------------------------------------------
declare -a ARGS=( "--model=${MODEL}" "--tensor-parallel=${TENSOR_PARALLEL}"
                  "--max-model-len=${MAX_MODEL_LEN}" "--dataset-gcs=${DATASET_GCS}"
                  "--output-bucket=${OUTPUT_BUCKET}" "--mode=${MODE}"
                  "--personas=${PERSONAS}" "--tool-call-parser=${TOOL_CALL_PARSER}" )
[[ -n "$GGUF_FILENAME"   ]] && ARGS+=( "--gguf-filename=${GGUF_FILENAME}" )
[[ -n "$TOKENIZER"       ]] && ARGS+=( "--tokenizer=${TOKENIZER}" )
[[ -n "$QUANTIZATION"    ]] && ARGS+=( "--quantization=${QUANTIZATION}" )
[[ -n "$REASONING_PARSER" ]] && ARGS+=( "--reasoning-parser=${REASONING_PARSER}" )
[[ "$FLATTEN_TRANSCRIPT" == "1" ]] && ARGS+=( "--flatten-transcript" )
[[ "$DISABLE_THINKING_BUDGET" == "1" ]] && ARGS+=( "--disable-thinking-budget" )
[[ "$EXPLICIT_TOOL_USE_PROMPT" == "1" ]] && ARGS+=( "--explicit-tool-use-prompt" )
[[ "$ENABLE_EXPERT_PARALLEL" == "1" ]] && ARGS+=( "--enable-expert-parallel" )
[[ -n "$KV_CACHE_DTYPE" ]] && ARGS+=( "--kv-cache-dtype=${KV_CACHE_DTYPE}" )
[[ -n "$MTP_NUM_TOKENS" ]] && ARGS+=( "--mtp-num-tokens=${MTP_NUM_TOKENS}" )
[[ "$TRUST_REMOTE_CODE" == "1" ]] && ARGS+=( "--trust-remote-code" )
[[ "$DEQUANTIZE_GGUF" == "1" ]] && ARGS+=( "--dequantize-gguf" )
[[ -n "$GPU_MEM_UTIL" ]] && ARGS+=( "--gpu-memory-utilization=${GPU_MEM_UTIL}" )
[[ -n "$RUN_TIMEOUT" ]] && ARGS+=( "--run-timeout=${RUN_TIMEOUT}" )
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
    [[ -n "$STRATEGY" ]] && echo "  strategy: ${STRATEGY}"
    [[ "$STRATEGY" == "FLEX_START" ]] && echo "  maxWaitDuration: ${MAX_WAIT}"
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

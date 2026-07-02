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
# Reasoning-CoT separator. When the model's reasoning delimiter matches a vLLM
# parser, the CoT is split into message.reasoning_content and the tool call
# surfaces cleanly in tool_calls. Pick per backbone:
#   QwQ-32B / Seneca (Qwen 2.5)  -> deepseek_r1   (matches <think>…</think>)
#   Foundation-Sec-8B            -> (leave EMPTY). Its tokenizer has no <think>
#       special token and no vLLM parser matches its output (verified:
#       reasoning_tokens=0 under minimax_m2). Leave the CoT in content and let
#       the adapter's content-fallback recover the tool call; pair with
#       --flatten-transcript + --disable-thinking-budget.
# Empty -> omit the flag.
REASONING_PARSER=""
TOKENIZER=""
GGUF_FILENAME=""
# Convert a GGUF-only release to fp16 safetensors and serve it UNQUANTIZED in
# vLLM instead of through the slow GGUF path. For models published only as GGUF
# (Seneca-x-QwQ-32B), vLLM's GGUF decode is ~2-3x slower than its native fp16
# kernels (no CUDA graphs / poor batched decode), which is the difference between
# finishing the 1500-unit run in the budget and not. Dequantizing yields fp16-of-Q4
# (no extra quality loss vs. the Q4 you'd otherwise serve) but unlocks the fast
# path. 32B fp16 ~=62GB fits 4xL4 (TP4); pair with a smaller --max-model-len so KV
# cache leaves room for useful concurrency. Requires --gguf-filename.
DEQUANTIZE_GGUF="0"
GPU_MEM_UTIL=""
# Wall-clock backstop for the socbench run itself (a hung run must never burn the
# GPU for days). Empty -> falls back to the RUN_TIMEOUT env, then 24h. Raise it
# (e.g. 47h) for an all-personas 32B run that legitimately exceeds a day so it
# isn't cut off mid-run with only partial results uploaded.
RUN_TIMEOUT_ARG=""
# Transcript/budget shims for models whose chat template can't drive an agentic
# loop. Foundation-Sec-8B has NO tool-role branch, mangles assistant history, and
# ships no tool-call parser, so it needs: flatten the transcript into one user
# message (so it sees tool results + its own prior calls) and DON'T send the vLLM
# thinking_token_budget param (no reasoning parser is matched, so the param would
# 400 on the V2 runner). Qwen/QwQ (Seneca) needs neither — leave both at 0.
FLATTEN_TRANSCRIPT="0"
DISABLE_THINKING_BUDGET="0"
# Explicit "actually call tools, don't narrate" scaffold (sec8 shim). Default off
# so capable models (Seneca, GLM, …) run on the neutral, leaderboard-fair scaffold.
EXPLICIT_TOOL_USE_PROMPT="0"
# Large-MoE serving knobs (e.g. GLM-5.2 W4A16): expert parallelism, fp8 KV-cache,
# MTP speculative decoding, and trust-remote-code for custom modeling. All default
# off/empty so they don't affect the dense open models (sec8/seneca).
ENABLE_EXPERT_PARALLEL="0"
KV_CACHE_DTYPE=""
MTP_NUM_TOKENS=""
TRUST_REMOTE_CODE="0"
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
        --reasoning-parser)  REASONING_PARSER="$2";  shift 2 ;;
        --tokenizer)         TOKENIZER="$2";         shift 2 ;;
        --gguf-filename)     GGUF_FILENAME="$2";     shift 2 ;;
        --flatten-transcript)      FLATTEN_TRANSCRIPT="1";      shift ;;
        --explicit-tool-use-prompt) EXPLICIT_TOOL_USE_PROMPT="1"; shift ;;
        --disable-thinking-budget) DISABLE_THINKING_BUDGET="1"; shift ;;
        --dequantize-gguf)         DEQUANTIZE_GGUF="1";         shift ;;
        --enable-expert-parallel)  ENABLE_EXPERT_PARALLEL="1";  shift ;;
        --kv-cache-dtype)          KV_CACHE_DTYPE="$2";         shift 2 ;;
        --mtp-num-tokens)          MTP_NUM_TOKENS="$2";         shift 2 ;;
        --trust-remote-code)       TRUST_REMOTE_CODE="1";       shift ;;
        --gpu-memory-utilization)  GPU_MEM_UTIL="$2";           shift 2 ;;
        --run-timeout)             RUN_TIMEOUT_ARG="$2";        shift 2 ;;
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
        --reasoning-parser=*)  REASONING_PARSER="${1#*=}";  shift ;;
        --tokenizer=*)         TOKENIZER="${1#*=}";         shift ;;
        --gguf-filename=*)     GGUF_FILENAME="${1#*=}";     shift ;;
        --kv-cache-dtype=*)        KV_CACHE_DTYPE="${1#*=}";    shift ;;
        --mtp-num-tokens=*)        MTP_NUM_TOKENS="${1#*=}";    shift ;;
        --gpu-memory-utilization=*) GPU_MEM_UTIL="${1#*=}";     shift ;;
        --run-timeout=*)           RUN_TIMEOUT_ARG="${1#*=}";   shift ;;
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

# Stable, API-facing model name (the HF repo id). For GGUF models, MODEL gets
# rewritten below to a local file path; we keep SERVED_NAME so vLLM serves under
# a clean alias and socbench (via OPEN_SOURCE_MODEL) requests that same alias.
# Without this, vLLM served the GGUF under its /tmp path while socbench asked for
# the config default -> 404 on every chat/completions call.
SERVED_NAME="$MODEL"

# ---------- GGUF handling (skipped in dry-run) ----------
# Two paths for a GGUF release:
#   (a) DEQUANTIZE_GGUF=1 -> load the GGUF via transformers (which reads config
#       from the GGUF metadata, so a config.json-less repo works), dequantize to
#       fp16, and save plain safetensors so vLLM serves it on the FAST native
#       path. The Qwen tokenizer (with the tool-calling chat template) is baked
#       into the output dir so vLLM picks up the right template.
#   (b) otherwise -> download the single .gguf file and point vLLM at it with
#       --quantization gguf (slower decode, but no conversion step).
# vLLM cannot resolve hf:// URIs or GGUF-only repos directly, hence the pre-step.
if [[ -n "$GGUF_FILENAME" && "$DRY_RUN" != "1" ]]; then
    if [[ "$DEQUANTIZE_GGUF" == "1" ]]; then
        FP16_DIR="/tmp/model-fp16"
        TOK_SRC="${TOKENIZER:-$MODEL}"
        echo "[entrypoint] dequantizing GGUF ${MODEL}/${GGUF_FILENAME} -> fp16 (${FP16_DIR}); tokenizer=${TOK_SRC}"
        # CPU-side dequant (GPUs are free until vLLM starts; 32B fp16 ~=62GB fits
        # the g2 host RAM). Fail HARD on any error — silently serving a broken or
        # half-written model would produce an all-zero run, the exact thing we are
        # trying to avoid.
        python3 -c "
import sys, torch
from transformers import AutoModelForCausalLM, AutoTokenizer
repo, gguf, out, tok_src = '${MODEL}', '${GGUF_FILENAME}', '${FP16_DIR}', '${TOK_SRC}'
print(f'[dequant] loading {repo}::{gguf} (this downloads + dequantizes the GGUF)...', flush=True)
model = AutoModelForCausalLM.from_pretrained(
    repo, gguf_file=gguf, torch_dtype=torch.float16,
    low_cpu_mem_usage=True,
)
print('[dequant] saving fp16 safetensors...', flush=True)
model.save_pretrained(out, safe_serialization=True, max_shard_size='5GB')
# Bake the tool-capable tokenizer/chat template into the served dir.
tok = AutoTokenizer.from_pretrained(tok_src)
tok.save_pretrained(out)
print('[dequant] done', flush=True)
" || { echo '[entrypoint] FATAL: GGUF->fp16 dequantization failed' >&2; exit 1; }
        # Sanity-check the conversion actually produced weights + a config before
        # we hand the dir to vLLM (a 0-byte / partial save must fail fast).
        if ! ls "${FP16_DIR}"/*.safetensors >/dev/null 2>&1 || [[ ! -f "${FP16_DIR}/config.json" ]]; then
            echo "[entrypoint] FATAL: ${FP16_DIR} missing safetensors/config after dequant" >&2
            exit 1
        fi
        MODEL="$FP16_DIR"
        # The GGUF is now plain fp16 safetensors. Drop a leftover "gguf" quant flag
        # and the --tokenizer override (tokenizer is baked into the dir). An explicit
        # online quant (e.g. fp8) is PRESERVED so vLLM quantizes the dequantized
        # weights at load (fp16 -> fp8 ~halves memory, frees KV for concurrency).
        [[ "$QUANTIZATION" == "gguf" ]] && QUANTIZATION=""
        TOKENIZER=""
        echo "[entrypoint] dequantized model ready: ${MODEL}"
    else
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
        --served-model-name "$SERVED_NAME"
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
    # Fraction of GPU memory vLLM may claim (weights + KV cache + activations).
    # Default (vLLM 0.9) is fine with headroom, but a 32B fp16 model on a single
    # 80GB A100 is ~64GB of weights — at 0.9 the leftover KV cache can be too
    # small to admit even one max-len sequence and engine init fails. Set
    # GPU_MEM_UTIL=0.95 there to free ~4GB more for KV. Unset -> vLLM default.
    [[ -n "$GPU_MEM_UTIL" ]] && VLLM_ARGS+=(--gpu-memory-utilization "$GPU_MEM_UTIL")
    # Reasoning models: split CoT into reasoning_content so the tool-call parser
    # sees a clean tool call instead of a prose+JSON blob (else every rendering
    # scores zero). Omitted when REASONING_PARSER is empty (non-reasoning models).
    [[ -n "$REASONING_PARSER" ]] && VLLM_ARGS+=(--reasoning-parser "$REASONING_PARSER")
    # Large-MoE knobs (GLM-5.2 W4A16). Expert parallelism shards the routed experts;
    # fp8 KV-cache halves KV memory; MTP speculative decoding speeds decode;
    # trust-remote-code loads the model's custom modeling code.
    [[ "$ENABLE_EXPERT_PARALLEL" == "1" ]] && VLLM_ARGS+=(--enable-expert-parallel)
    [[ -n "$KV_CACHE_DTYPE" ]] && VLLM_ARGS+=(--kv-cache-dtype "$KV_CACHE_DTYPE")
    # Build the MTP speculative-config JSON here (a bash array element preserves the
    # quotes for vLLM; passing raw JSON through the job-spec YAML would not).
    [[ -n "$MTP_NUM_TOKENS" ]] && VLLM_ARGS+=(--speculative-config "{\"method\": \"mtp\", \"num_speculative_tokens\": ${MTP_NUM_TOKENS}}")
    [[ "$TRUST_REMOTE_CODE" == "1" ]] && VLLM_ARGS+=(--trust-remote-code)

    # vLLM v0.23.0 defaults to the V2 model runner, which 400s on the
    # thinking_token_budget request param the adapter sends to hard-cap reasoning
    # ("thinking_token_budget is not yet supported by the V2 model runner" ->
    # every /v1/chat/completions fails -> all-zero scores). Pin the V1 runner
    # ONLY when that param is actually in play — reasoning parser set AND budget
    # not disabled — so reasoning models get V1 while non-reasoning models (which
    # never send the param) and GLM-5.2 (which disables the budget and needs V2
    # for MTP/compressed-tensors) stay on V2.
    [[ -n "$REASONING_PARSER" && "$DISABLE_THINKING_BUDGET" != "1" ]] && export VLLM_USE_V2_MODEL_RUNNER=0

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
export OPEN_SOURCE_MODEL="$SERVED_NAME"
export OPEN_SOURCE_API_KEY=""
# Per-request HTTP deadline for the open_source adapter. Self-hosted reasoning
# models under concurrent serving take minutes per turn; the old hard-coded 120s
# client timeout fired on every request, marking each rendering adapter_fatal
# and producing all-zero scores. Default 600s; override with OPEN_SOURCE_TIMEOUT_SECONDS.
export OPEN_SOURCE_TIMEOUT_SECONDS="${OPEN_SOURCE_TIMEOUT_SECONDS:-600}"
# Template-agnostic transcript + thinking-budget kill-switch (sec-8b path). The
# adapter reads these; they are no-ops for models that don't set the flags.
[[ "$FLATTEN_TRANSCRIPT" == "1" ]] && export OPEN_SOURCE_FLATTEN_TRANSCRIPT=1
[[ "$DISABLE_THINKING_BUDGET" == "1" ]] && export OPEN_SOURCE_DISABLE_THINKING_BUDGET=1
echo "[entrypoint] open_source shims: flatten_transcript=${FLATTEN_TRANSCRIPT} disable_thinking_budget=${DISABLE_THINKING_BUDGET} reasoning_parser=${REASONING_PARSER:-<none>}"
# Per-turn raw-response dump (api_finish_reason, raw_tool_calls, content,
# reasoning_content, content_fallback flag). Written under runs/ so it uploads
# with the rest of the artifacts — lets us verify tool-call parsing / fallback
# without re-running. The adapter only writes when this env is set.
mkdir -p runs
export OPEN_SOURCE_DEBUG_DUMP="runs/os_debug_dump.jsonl"

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
[[ "$EXPLICIT_TOOL_USE_PROMPT" == "1" ]] && BENCH_ARGS+=(--explicit-tool-use-prompt)
# Self-hosted: tell the loop the served context window so it force-submits before
# the transcript overflows max_model_len (→ 400 → adapter_fatal). Always known here.
BENCH_ARGS+=(--context-window-tokens "$MAX_MODEL_LEN")

# Wall-clock backstop: a hung run must never burn the GPU indefinitely (Vertex's
# default job timeout is 7 days). Default 24h; override with RUN_TIMEOUT.
RUN_TIMEOUT="${RUN_TIMEOUT_ARG:-${RUN_TIMEOUT:-24h}}"
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

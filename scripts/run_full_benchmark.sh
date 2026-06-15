#!/usr/bin/env bash
#
# Launch the full socbench benchmark as three parallel, per-provider jobs.
#
# Why three jobs (not one --providers a,b,c run)?
#   The CLI's --cost-budget-usd is a single per-RUN value, so per-provider
#   budget caps (matching the console API usage limits) require separate runs.
#   Each provider also gets its own run_dir / summary.json; merge afterwards
#   with `socbench aggregate` or the comparison reader.
#
# Per-provider caps mirror the console API usage limits you set:
#   openai $700 · gemini $700 · anthropic $900
#
# Usage:
#   export OPENAI_API_KEY=...  ANTHROPIC_API_KEY=...  GEMINI_API_KEY=...
#   scripts/run_full_benchmark.sh                 # all three, default hash
#   DATASET_HASH=<hash> scripts/run_full_benchmark.sh
#   PROVIDERS="openai,gemini" scripts/run_full_benchmark.sh   # subset
#
set -euo pipefail

# --- config (override via env) ---------------------------------------------
DATASET_HASH="${DATASET_HASH:-4bc5181b382427498acf86da3a1ad0f2}"
MODE="${MODE:-full}"
ABLATION="${ABLATION:-main}"
PERSONAS="${PERSONAS:-all}"
PROVIDERS="${PROVIDERS:-openai,gemini,anthropic}"

OPENAI_BUDGET="${OPENAI_BUDGET:-700}"
GEMINI_BUDGET="${GEMINI_BUDGET:-700}"
ANTHROPIC_BUDGET="${ANTHROPIC_BUDGET:-900}"

LOG_DIR="${LOG_DIR:-runs/_launch_logs}"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# --- preflight: required API keys for the selected providers ----------------
declare -A KEY_FOR=( [openai]=OPENAI_API_KEY [gemini]=GEMINI_API_KEY [anthropic]=ANTHROPIC_API_KEY )
declare -A BUDGET_FOR=( [openai]="$OPENAI_BUDGET" [gemini]="$GEMINI_BUDGET" [anthropic]="$ANTHROPIC_BUDGET" )

IFS=',' read -r -a SELECTED <<< "$PROVIDERS"
for p in "${SELECTED[@]}"; do
  key_var="${KEY_FOR[$p]:-}"
  if [[ -z "$key_var" ]]; then
    echo "ERROR: unknown provider '$p' (expected openai|gemini|anthropic)" >&2
    exit 2
  fi
  if [[ -z "${!key_var:-}" ]]; then
    echo "ERROR: $key_var is not set (required for provider '$p')" >&2
    exit 2
  fi
done

echo "Launching full benchmark"
echo "  dataset_hash : $DATASET_HASH"
echo "  mode         : $MODE   ablation: $ABLATION   personas: $PERSONAS"
echo "  providers    : ${SELECTED[*]}"
echo "  log dir      : $LOG_DIR"
echo

# --- launch one job per provider, in parallel ------------------------------
declare -A PID_FOR
for p in "${SELECTED[@]}"; do
  budget="${BUDGET_FOR[$p]}"
  log="$LOG_DIR/${STAMP}_${p}.log"
  echo "  -> $p  (cap \$$budget)  log: $log"
  uv run socbench --log-format human run \
    --dataset-hash "$DATASET_HASH" \
    --providers "$p" \
    --personas "$PERSONAS" \
    --mode "$MODE" \
    --ablation "$ABLATION" \
    --cost-budget-usd "$budget" \
    >"$log" 2>&1 &
  PID_FOR[$p]=$!
done

echo
echo "All jobs launched. Waiting for completion (tail the logs above to watch)..."

# --- wait + report ----------------------------------------------------------
rc=0
for p in "${SELECTED[@]}"; do
  if wait "${PID_FOR[$p]}"; then
    echo "  [OK]   $p"
    grep -E '"run_dir"|"total_cost_usd"|"aborted_for_budget"' "$LOG_DIR/${STAMP}_${p}.log" || true
  else
    echo "  [FAIL] $p (exit $?); see $LOG_DIR/${STAMP}_${p}.log" >&2
    rc=1
  fi
done

echo
if [[ $rc -eq 0 ]]; then
  echo "All provider jobs finished. Next: aggregate / compare the per-provider summaries."
else
  echo "One or more jobs failed; inspect the logs above." >&2
fi
exit $rc

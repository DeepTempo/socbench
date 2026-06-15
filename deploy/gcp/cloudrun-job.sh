#!/usr/bin/env bash
# Launch the socbench benchmark as a Cloud Run Job (serverless — no GKE cluster).
#
# Reuses the same image + entrypoint as the GKE path; only the launcher differs.
# The container entrypoint (deploy/gcp/entrypoint.sh) pulls the dataset from GCS,
# builds the index, runs the benchmark, and uploads runs/ back to GCS.
#
# What this does:
#   1. (unless SKIP_BUILD=1) builds + pushes a linux/amd64 image via Cloud Build
#   2. resolves a service account to run as (reuses an existing one)
#   3. `gcloud run jobs deploy` (create-or-update) with inline env + API keys
#   4. executes the job and prints follow/log commands
#
# Prereqs: gcloud authenticated to the project; LLM API keys exported in your
# shell (ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY). Keys are passed
# inline as env vars at deploy time (per request) — not stored in Secret Manager.
#
# Per-provider example (three independent jobs sharing one image):
#   export TAG=$(git rev-parse --short=12 HEAD)
#   PROVIDERS=anthropic JOB_NAME=socbench-full-anthropic COST_BUDGET_USD=850 bash deploy/gcp/cloudrun-job.sh
#   SKIP_BUILD=1 PROVIDERS=openai JOB_NAME=socbench-full-openai COST_BUDGET_USD=500 bash deploy/gcp/cloudrun-job.sh
#   SKIP_BUILD=1 PROVIDERS=gemini JOB_NAME=socbench-full-gemini COST_BUDGET_USD=500 bash deploy/gcp/cloudrun-job.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PROJECT="${PROJECT:-your-gcp-project}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-socbench}"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}"
TAG="${TAG:-$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
IMAGE="${IMAGE:-${REGISTRY}/socbench:${TAG}}"

PROVIDERS="${PROVIDERS:?set PROVIDERS, e.g. 'anthropic' or 'anthropic,openai,gemini'}"
MODE="${MODE:-full}"
PERSONAS="${PERSONAS:-all}"
BUDGET="${COST_BUDGET_USD:-500}"
DATASET_GCS="${DATASET_GCS:-gs://your-bucket/benchmark-v0/combined/benchmark-v0-canonical.parquet}"
RESULTS_GCS="${RESULTS_GCS:-gs://your-bucket/benchmark-v0/results}"
JOB_NAME="${JOB_NAME:-socbench-${MODE}}"
# Cloud Run resource names: DNS-1123, lowercase, <=63 (leave room for exec suffix).
JOB_NAME="$(printf '%s' "$JOB_NAME" | tr '[:upper:]_' '[:lower:]-' | cut -c1-49)"

CPU="${CPU:-2}"
MEMORY="${MEMORY:-8Gi}"
TASK_TIMEOUT="${TASK_TIMEOUT:-24h}"
SKIP_BUILD="${SKIP_BUILD:-0}"

echo "=== socbench Cloud Run Job ==="
echo "  project=${PROJECT} region=${REGION}"
echo "  image=${IMAGE}"
echo "  job=${JOB_NAME} providers=${PROVIDERS} mode=${MODE} personas=${PERSONAS} budget=\$${BUDGET}"

# --- 1. build image (skippable so sibling jobs reuse the same tag) -----------
if [ "$SKIP_BUILD" = "1" ]; then
    echo "=== SKIP_BUILD=1 — reusing existing image ${IMAGE} ==="
else
    echo "=== building image via Cloud Build ==="
    gcloud builds submit "$ROOT" --project "$PROJECT" \
        --config "$ROOT/deploy/gcp/cloudbuild.yaml" \
        --substitutions "_IMAGE=${IMAGE}"
fi

# --- 2. resolve a service account to run as (reuse existing) -----------------
if [ -z "${SERVICE_ACCOUNT:-}" ]; then
    SERVICE_ACCOUNT="$(gcloud iam service-accounts list --project "$PROJECT" \
        --format='value(email)' --filter='email~socbench' 2>/dev/null | head -1 || true)"
    if [ -z "$SERVICE_ACCOUNT" ]; then
        PNUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
        SERVICE_ACCOUNT="${PNUM}-compute@developer.gserviceaccount.com"
    fi
fi
echo "  service-account=${SERVICE_ACCOUNT}"

# --- 3. assemble env (config + inline API keys) ------------------------------
# gcloud --set-env-vars uses ',' as the default separator; switch to '@@' via
# the "^DELIM^" prefix so values are never mis-split.
declare -a KV=(
    "DATASET_GCS=${DATASET_GCS}"
    "RESULTS_GCS=${RESULTS_GCS}"
    "PROVIDERS=${PROVIDERS}"
    "MODE=${MODE}"
    "PERSONAS=${PERSONAS}"
    "COST_BUDGET_USD=${BUDGET}"
    "GOOGLE_CLOUD_PROJECT=${PROJECT}"
)
[ -n "${ANTHROPIC_API_KEY:-}" ] && KV+=("ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}")
[ -n "${OPENAI_API_KEY:-}" ] && KV+=("OPENAI_API_KEY=${OPENAI_API_KEY}")
GEM_KEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}"
[ -n "$GEM_KEY" ] && KV+=("GEMINI_API_KEY=${GEM_KEY}")

DELIM='@@'
ENVS=""
for kv in "${KV[@]}"; do
    if [ -z "$ENVS" ]; then ENVS="$kv"; else ENVS="${ENVS}${DELIM}${kv}"; fi
done
SET_ENV="^${DELIM}^${ENVS}"

# --- 4. deploy (create-or-update) and execute --------------------------------
echo "=== deploying + executing Cloud Run Job ${JOB_NAME} ==="
gcloud run jobs deploy "$JOB_NAME" \
    --project "$PROJECT" \
    --region "$REGION" \
    --image "$IMAGE" \
    --service-account "$SERVICE_ACCOUNT" \
    --cpu "$CPU" \
    --memory "$MEMORY" \
    --max-retries 0 \
    --task-timeout "$TASK_TIMEOUT" \
    --tasks 1 \
    --set-env-vars "$SET_ENV" \
    --execute-now

cat <<EOF

=== launched ===
Watch executions:
  gcloud run jobs executions list --job ${JOB_NAME} --region ${REGION} --project ${PROJECT}
Tail logs (latest execution):
  gcloud beta run jobs executions logs read \$(gcloud run jobs executions list --job ${JOB_NAME} --region ${REGION} --project ${PROJECT} --format='value(name)' --limit 1) --region ${REGION} --project ${PROJECT}
Console:
  https://console.cloud.google.com/run/jobs/details/${REGION}/${JOB_NAME}/executions?project=${PROJECT}
Results upload to:
  ${RESULTS_GCS}/runs/
EOF

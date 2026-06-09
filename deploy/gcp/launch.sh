#!/usr/bin/env bash
# Launch the full socbench benchmark as a one-shot GKE Job on the tempo cluster.
#
# What it does:
#   1. builds + pushes a linux/amd64 image to Artifact Registry (via Cloud Build)
#   2. creates/updates the `socbench-llm-keys` secret from your local env vars
#   3. detects which providers have keys and applies the Job
#   4. prints commands to follow logs and where results land in GCS
#
# Prereqs: gcloud + kubectl authenticated to project prod-loglm; LLM API keys
# exported in your shell (ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PROJECT="${PROJECT:-prod-loglm}"
REGION="${REGION:-us-central1}"
NAMESPACE="${NAMESPACE:-tempo}"
AR_REPO="${AR_REPO:-socbench}"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}"
TAG="${TAG:-$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
IMAGE="${IMAGE:-${REGISTRY}/socbench:${TAG}}"

MODE="${MODE:-full}"
BUDGET="${COST_BUDGET_USD:-500}"
DATASET_GCS="${DATASET_GCS:-gs://tempo-datasets-001/benchmark-v0/combined/benchmark-v0-canonical.parquet}"
RESULTS_GCS="${RESULTS_GCS:-gs://tempo-datasets-001/benchmark-v0/results}"
JOB_NAME="${JOB_NAME:-socbench-${MODE}-${TAG}}"
# k8s names must be DNS-1123: lowercase alnum + '-'.
JOB_NAME="$(printf '%s' "$JOB_NAME" | tr '[:upper:]_' '[:lower:]-' | cut -c1-63)"

echo "=== socbench GKE launch ==="
echo "  project=${PROJECT} region=${REGION} ns=${NAMESPACE}"
echo "  image=${IMAGE}"
echo "  mode=${MODE} budget=\$${BUDGET}"
echo "  dataset=${DATASET_GCS}"
echo "  results=${RESULTS_GCS}"
echo "  job=${JOB_NAME}"

# --- 1. detect providers from local env --------------------------------------
declare -a SECRET_ARGS=()
declare -a PROVS=()
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    SECRET_ARGS+=("--from-literal=ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}")
    PROVS+=("anthropic")
fi
if [ -n "${OPENAI_API_KEY:-}" ]; then
    SECRET_ARGS+=("--from-literal=OPENAI_API_KEY=${OPENAI_API_KEY}")
    PROVS+=("openai")
fi
GEM_KEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}"
if [ -n "$GEM_KEY" ]; then
    SECRET_ARGS+=("--from-literal=GEMINI_API_KEY=${GEM_KEY}")
    PROVS+=("gemini")
fi
if [ "${#PROVS[@]}" -eq 0 ]; then
    echo "error: no provider API keys found in env (need ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY)" >&2
    exit 1
fi
PROVIDERS="${PROVIDERS:-$(IFS=,; echo "${PROVS[*]}")}"
echo "  providers=${PROVIDERS}"

# --- 2. cluster credentials ---------------------------------------------------
export USE_GKE_GCLOUD_AUTH_PLUGIN=True
if ! kubectl cluster-info >/dev/null 2>&1; then
    CLUSTER="${GKE_CLUSTER:-}"
    if [ -z "$CLUSTER" ]; then
        CLUSTER="$(gcloud container clusters list --project "$PROJECT" \
            --format='value(name)' | head -1)"
    fi
    LOCATION="$(gcloud container clusters list --project "$PROJECT" \
        --filter="name=${CLUSTER}" --format='value(location)' | head -1)"
    echo "  getting credentials for cluster=${CLUSTER} location=${LOCATION}"
    gcloud container clusters get-credentials "$CLUSTER" \
        --location "$LOCATION" --project "$PROJECT" >/dev/null
fi

# --- 3. build + push image (Cloud Build = amd64, no local docker needed) ------
# Dockerfile is at deploy/gcp/Dockerfile, so build via cloudbuild.yaml (the
# `--tag` shortcut requires a root Dockerfile).
echo "=== building image via Cloud Build ==="
gcloud builds submit "$ROOT" \
    --project "$PROJECT" \
    --config "$ROOT/deploy/gcp/cloudbuild.yaml" \
    --substitutions "_IMAGE=${IMAGE}"

# --- 4. upsert the API-key secret ---------------------------------------------
echo "=== upserting secret socbench-llm-keys ==="
kubectl create secret generic socbench-llm-keys \
    --namespace "$NAMESPACE" \
    "${SECRET_ARGS[@]}" \
    --dry-run=client -o yaml | kubectl apply -f -

# --- 5. render + apply the Job ------------------------------------------------
echo "=== applying Job ${JOB_NAME} ==="
kubectl delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
sed -e "s|__JOB_NAME__|${JOB_NAME}|g" \
    -e "s|__IMAGE__|${IMAGE}|g" \
    -e "s|__DATASET_GCS__|${DATASET_GCS}|g" \
    -e "s|__RESULTS_GCS__|${RESULTS_GCS}|g" \
    -e "s|__PROVIDERS__|${PROVIDERS}|g" \
    -e "s|__MODE__|${MODE}|g" \
    -e "s|__BUDGET__|${BUDGET}|g" \
    "${ROOT}/deploy/gcp/job.yaml" | kubectl apply -f -

cat <<EOF

=== launched ===
Follow logs:
  kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}
Watch status:
  kubectl get job/${JOB_NAME} -n ${NAMESPACE} -w
Results will be uploaded to:
  ${RESULTS_GCS}/runs/
You can close your laptop — the Job runs in-cluster.
EOF

# socbench Vertex AI Custom Job image — local GPU inference + benchmark.
#
# Built on the official vLLM image so CUDA / torch / xFormers are pre-baked.
# The container starts an OpenAI-compatible vLLM server on localhost:8000,
# waits for it to be ready, then runs socbench against it.
#
# Build (from repo root):
#   docker build -f deploy/vertex/Dockerfile -t gcr.io/PROJECT/socbench-vertex:latest .
#
# Submit to Vertex AI:
#   gcloud ai custom-jobs create \
#     --region=us-central1 \
#     --display-name=sec-8b-benchmark \
#     --worker-pool-spec=machine-type=g2-standard-8,accelerator-type=NVIDIA_L4,accelerator-count=1,container-image-uri=gcr.io/PROJECT/socbench-vertex:latest \
#     --args="--model=fdtn-ai/Foundation-Sec-8B-Reasoning" \
#     --args="--output-bucket=gs://my-benchmark-results"
#
# The entrypoint supports the following CLI flags (all optional except --model):
#   --model             Hugging Face model id or local path (required)
#   --quantization      vLLM quantization: awq, gptq, fp8, etc. (default: none)
#   --max-model-len     Max context length in tokens (default: 8192)
#   --dtype             torch dtype: auto, float16, bfloat16 (default: auto)
#   --tensor-parallel   Number of GPUs for tensor parallelism (default: 1)
#   --dataset-gcs       gs:// URI of the canonical parquet (default: built-in sample)
#   --output-bucket     gs:// prefix to upload runs/ under (required)
#   --mode              smoke|full (default: full)
#   --personas          all or CSV (default: all)
#   --cost-budget-usd   abort cleanly when reached (default: 0)

# Pinned (was :latest) for reproducible benchmark runs — a moving base image
# would silently change tool-call parsing / structured-output behaviour between
# runs. v0.23.0 (V1 engine) ships StructuredOutputsConfig(backend='auto') ON by
# default, so a named `tool_choice` (the forced submit_assessment path) is already
# grammar-constrained to the JSON schema — matching the hosted-API contract with
# NO extra flags. Do not add the legacy --guided-decoding-backend flag: it was
# renamed to the structured-outputs config in this version and will error.
FROM vllm/vllm-openai:v0.23.0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VLLM_USAGE_STATS_SOURCE=production-docker \
    VLLM_NO_USAGE_STATS=1

WORKDIR /app

# Install socbench and GCS filesystem support.
# We copy the source first so `pip install .` picks up the local tree.
COPY . /app
# `gguf` lets transformers read GGUF metadata/weights for the optional
# --dequantize-gguf path (GGUF -> fp16 safetensors so vLLM serves the fast
# native kernels instead of the slow GGUF decode path).
RUN pip install ".[providers]" gcsfs httpx gguf accelerate \
    && chmod +x /app/deploy/vertex/entrypoint.sh

# vLLM listens here; socbench talks to localhost:8000/v1 via the open_source adapter.
EXPOSE 8000

ENTRYPOINT ["/app/deploy/vertex/entrypoint.sh"]

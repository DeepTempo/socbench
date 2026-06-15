# socbench batch image — runs the full benchmark as a one-shot GKE Job.
# Built for linux/amd64 (GKE nodes); see deploy/gcp/launch.sh.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build deps are not needed: polars/duckdb/pyarrow ship manylinux wheels.
COPY . /app

# Install socbench with the real provider SDKs plus gcsfs for GCS I/O.
RUN pip install ".[providers]" gcsfs \
    && chmod +x /app/deploy/gcp/entrypoint.sh

ENTRYPOINT ["/app/deploy/gcp/entrypoint.sh"]

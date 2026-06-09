#!/usr/bin/env python3
"""Build one combined, canonical NetFlow parquet from the GCS benchmark captures.

Reads the benign + Stratosphere malware captures published under
``gs://tempo-datasets-001/benchmark-v0/`` and normalizes them into socbench's
canonical column set (see ``config/schema.json``), concatenated into a single
parquet so ``socbench build-index`` produces one mixed-population dataset.

Per-flow labels are preserved from each capture's own ``Label`` / ``attack_type``
columns — the malware captures contain individually-labeled benign background
traffic, so labeling is per-flow rather than per-file.

A single ``source`` column records which capture each flow came from. IP
addresses are kept raw (no per-capture namespacing), so identical private IPs
appearing in different captures share host/pair identity downstream.

Usage
-----

    python scripts/build_from_gcs.py \\
        --output gs://tempo-datasets-001/benchmark-v0/combined/benchmark-v0-canonical.parquet \\
        --local-staging data/benchmark-v0.parquet \\
        --project prod-loglm

Credentials come from Application Default Credentials (``gcloud auth
application-default login``); the GCS client libraries pick them up
automatically.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import gcsfs
import polars as pl
import pyarrow.parquet as pq

DEFAULT_PROJECT = "prod-loglm"
DEFAULT_BASE = "tempo-datasets-001/benchmark-v0"
DEFAULT_OUTPUT = f"gs://{DEFAULT_BASE}/combined/benchmark-v0-canonical.parquet"

# capture name -> (gcs object path relative to bucket, numeric-label column name)
BENIGN_FILES = {
    "normal-https-website": "benign/normal-HTTPS-website-traffic.parquet",
    "normal-at-home-linux": "benign/normal-at-home-user-traffic-linux.parquet",
    "normal-university-linux": "benign/normal-university-user-traffic-linux.parquet",
    "normal-xdsl-linux": "benign/normal-xDSL-user-linux.parquet",
}
MALWARE_FILES = {
    "malware_1_1": "stratosphere/malware_1_1.parquet",
    "malware_34_1": "stratosphere/malware_34_1.parquet",
    "malware_3_1": "stratosphere/malware_3_1.parquet",
    "malware_8_1": "stratosphere/malware_8_1.parquet",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="Destination gs:// URI.")
    p.add_argument(
        "--local-staging",
        type=Path,
        default=Path("data/benchmark-v0.parquet"),
        help="Local path the combined parquet is written to before upload.",
    )
    p.add_argument("--project", default=DEFAULT_PROJECT, help="GCP project for billing.")
    p.add_argument("--bucket-base", default=DEFAULT_BASE, help="bucket/prefix of sources.")
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="Write only the local staging file; skip the GCS upload.",
    )
    return p.parse_args(argv)


def _col_or_null(df: pl.DataFrame, name: str, dtype: pl.DataType) -> pl.Expr:
    """``df[name]`` cast to ``dtype`` when present, else a typed null literal."""
    if name in df.columns:
        return pl.col(name).cast(dtype)
    return pl.lit(None, dtype=dtype)


def to_canonical(df: pl.DataFrame, *, source: str) -> pl.DataFrame:
    """Map one source capture onto the canonical column set + labels + source.

    - ``timestamp`` (Datetime[ns]) -> ``ts_start`` epoch seconds (float).
    - bytes: ``fwd_bytes`` -> ``bytes_out``, ``bwd_bytes`` -> ``bytes_in``.
    - packets: ``fwd_pkts`` / ``bwd_pkts`` -> ``pkts_out`` / ``pkts_in``
      (null where the capture has no packet columns, e.g. benign files).
    - ``protocol`` rendered as string; ``"unknown"`` where absent.
    - ``flow_dur`` carried into ``flow_duration_ms`` as-is.
    - ``sampling_rate`` defaulted to 1.
    - per-flow ``Label`` (numeric) and ``Attack`` (family string from
      ``attack_type``) preserved for the index's label inference.
    """
    label_col = "Label" if "Label" in df.columns else "label"
    return df.select(
        pl.col("src_ip").cast(pl.Utf8),
        pl.col("dest_ip").cast(pl.Utf8).alias("dst_ip"),
        (pl.col("timestamp").dt.epoch(time_unit="ns").cast(pl.Float64) / 1e9).alias("ts_start"),
        (
            pl.col("protocol").cast(pl.Utf8)
            if "protocol" in df.columns
            else pl.lit("unknown")
        ).alias("protocol"),
        pl.col("src_port").cast(pl.Int64),
        pl.col("dest_port").cast(pl.Int64).alias("dst_port"),
        pl.col("bwd_bytes").cast(pl.Float64).alias("bytes_in"),
        pl.col("fwd_bytes").cast(pl.Float64).alias("bytes_out"),
        _col_or_null(df, "bwd_pkts", pl.Float64).alias("pkts_in"),
        _col_or_null(df, "fwd_pkts", pl.Float64).alias("pkts_out"),
        pl.lit("").alias("tcp_flags"),
        pl.col("flow_dur").cast(pl.Float64).alias("flow_duration_ms"),
        pl.lit(1, dtype=pl.Int64).alias("sampling_rate"),
        pl.col(label_col).cast(pl.Int64).alias("Label"),
        pl.col("attack_type").cast(pl.Utf8).alias("Attack"),
        pl.lit(source).alias("source"),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fs = gcsfs.GCSFileSystem(project=args.project)

    frames: list[pl.DataFrame] = []
    sources = {**BENIGN_FILES, **MALWARE_FILES}
    for source, rel in sources.items():
        gcs_path = f"{args.bucket_base}/{rel}"
        print(f"reading {source:<26} gs://{gcs_path}", file=sys.stderr)
        table = pq.read_table(gcs_path, filesystem=fs)
        df = pl.from_arrow(table)
        if not isinstance(df, pl.DataFrame):  # pragma: no cover - defensive
            raise TypeError(f"{source}: expected DataFrame, got {type(df)}")
        frames.append(to_canonical(df, source=source))

    combined = pl.concat(frames, how="vertical")

    args.local_staging.parent.mkdir(parents=True, exist_ok=True)
    combined.write_parquet(args.local_staging, compression="zstd", statistics=True)

    total = combined.height
    mal = int(combined.filter(pl.col("Label") > 0).height)
    print(
        f"\nwrote {args.local_staging} "
        f"({total:,} flows; {mal:,} malicious / {total - mal:,} benign; "
        f"{mal / total:.1%} malicious)",
        file=sys.stderr,
    )
    by_source = (
        combined.group_by("source")
        .agg(
            flows=pl.len(),
            malicious=pl.col("Label").gt(0).sum(),
        )
        .sort("source")
    )
    print(by_source, file=sys.stderr)

    if not args.no_upload:
        dest = args.output[len("gs://") :] if args.output.startswith("gs://") else args.output
        print(f"\nuploading -> gs://{dest}", file=sys.stderr)
        fs.put(str(args.local_staging), dest)
        print("upload complete", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

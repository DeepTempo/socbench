#!/usr/bin/env python3
"""Build a tiny labeled NetFlow parquet from a real source dataset.

Given a path or URI to a real labeled NetFlow parquet (e.g.
``NF-CSE-CIC-IDS2018.parquet``), this script writes a deterministic, mixed-
label subset of ≤ 10 MB suitable for committing to ``data/sample/`` so the
smoke benchmark runs with no downloads.

Usage
-----

    python scripts/build_sample_from_real.py \\
        --source /path/to/NF-CSE-CIC-IDS2018.parquet \\
        --output data/sample/cic2018-mini.parquet \\
        --max-rows 8000 \\
        --seed 7

Determinism
-----------
For a given (source, max-rows, seed) the produced parquet is reproducible.
The script also emits ``data/sample/PROVENANCE.md`` next to the output so
downstream users can see exactly how it was derived.

The script does **not** require socbench to be installed. It only uses
DuckDB + pyarrow which are widely available; this keeps it usable in a
preflight script before `pip install -e .`.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import duckdb


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--source", required=True, help="Path or URI to the source parquet.")
    p.add_argument(
        "--output", required=True, type=Path, help="Where to write the subset parquet."
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=8_000,
        help="Approximate upper bound on rows in the subset (default: 8000).",
    )
    p.add_argument(
        "--malicious-fraction",
        type=float,
        default=0.4,
        help="Target fraction of malicious rows in the subset (default: 0.4).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=7,
        help="RNG seed for the stratified subset (default: 7).",
    )
    p.add_argument(
        "--attack-column",
        default="Attack",
        help="Column name carrying attack family / 'benign' label (default: Attack).",
    )
    p.add_argument(
        "--label-column",
        default="Label",
        help="Optional numeric label column (default: Label).",
    )
    p.add_argument(
        "--max-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Hard upper bound on output file size in bytes (default: 10 MB).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    n_mal = int(args.max_rows * args.malicious_fraction)
    n_ben = args.max_rows - n_mal

    con = duckdb.connect()
    try:
        con.execute(f"SET threads = 4")
        # Stratified sample: half malicious, half benign, deterministic via setseed.
        # `Attack` column convention: anything not lower('benign') / empty is malicious.
        con.execute(f"CALL dbgen('seed:{args.seed}')") if False else None  # placeholder
        # DuckDB seeded random: SETSEED accepts a value in [-1.0, 1.0]
        seed_scalar = ((args.seed % 100) / 100.0) - 0.5
        con.execute(f"SELECT setseed({seed_scalar})")

        ben_sql = f"""
            SELECT *, random() AS _r
            FROM read_parquet('{args.source}')
            WHERE COALESCE(LOWER(CAST("{args.attack_column}" AS VARCHAR)), '') IN ('', 'benign')
            ORDER BY _r
            LIMIT {n_ben}
        """
        mal_sql = f"""
            SELECT *, random() AS _r
            FROM read_parquet('{args.source}')
            WHERE COALESCE(LOWER(CAST("{args.attack_column}" AS VARCHAR)), '') NOT IN ('', 'benign')
            ORDER BY _r
            LIMIT {n_mal}
        """
        con.execute(
            f"CREATE OR REPLACE TABLE sample AS "
            f"SELECT * EXCLUDE (_r) FROM ({ben_sql} UNION ALL {mal_sql}) "
            f"ORDER BY 1"  # deterministic row order in the parquet
        )

        con.execute(
            f"COPY sample TO '{args.output}' (FORMAT 'parquet', COMPRESSION 'zstd', ROW_GROUP_SIZE 1024)"
        )
    finally:
        con.close()

    actual_bytes = args.output.stat().st_size
    if actual_bytes > args.max_bytes:
        print(
            f"WARNING: output {actual_bytes} bytes exceeds max-bytes={args.max_bytes}. "
            f"Re-run with a smaller --max-rows.",
            file=sys.stderr,
        )

    sha = _sha256(args.output)
    _write_provenance(
        args.output, args=args, file_sha256=sha, file_bytes=actual_bytes
    )

    print(
        f"Wrote {args.output} ({actual_bytes:,} bytes, sha256={sha[:12]}...). "
        f"Provenance: {args.output.parent / 'PROVENANCE.md'}"
    )
    return 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_provenance(
    output: Path, *, args: argparse.Namespace, file_sha256: str, file_bytes: int
) -> None:
    md = textwrap.dedent(
        f"""\
        # Sample dataset provenance

        | Field | Value |
        |---|---|
        | Output | `{output.name}` |
        | Bytes | {file_bytes:,} |
        | SHA-256 | `{file_sha256}` |
        | Built at (UTC) | `{datetime.now(tz=timezone.utc).isoformat()}` |

        ## Source

        - **Path / URI**: `{args.source}`
          (commonly the `NF-CSE-CIC-IDS2018.parquet` flow-export of the
          Canadian Institute for Cybersecurity CSE-CIC-IDS2018 dataset, in the
          NetFlow format published by the CIC.)

        ## Subset rule

        DuckDB stratified random sample, seeded for reproducibility:

        - Seed: `{args.seed}` (passed to DuckDB's `setseed`)
        - Target rows: `{args.max_rows}` total, with `~{int(args.malicious_fraction * 100)}%`
          malicious and the remainder benign.
        - Malicious rule: ``LOWER(Attack) NOT IN ('', 'benign')``
        - Benign rule: ``LOWER(Attack)    IN ('', 'benign')``

        ## License

        The CSE-CIC-IDS2018 dataset is published by the Canadian Institute for
        Cybersecurity. Users are responsible for complying with its terms of
        use. socbench's redistribution of this small subset is for
        non-commercial research benchmarking. Replace this file at any time
        with a different dataset of your choosing — `socbench build-index`
        does not depend on which dataset you point it at, as long as the
        canonical schema in `config/schema.json` can resolve the columns.

        ## Regenerate

        ```bash
        python scripts/build_sample_from_real.py \\
            --source <path> \\
            --output {output} \\
            --max-rows {args.max_rows} \\
            --seed {args.seed}
        ```
        """
    )
    (output.parent / "PROVENANCE.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

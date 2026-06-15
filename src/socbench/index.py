"""Step A — corpus index builder.

Pipeline:

    sources (parquet) ──► normalize_parquet ──► assign_flow_ids
                                   │
                                   ├─► derive_pairs / derive_hosts
                                   ├─► assign_eval_units
                                   └─► hosts_rollup / pair_stats_rollup
                                                       │
                                                       ▼
                                            content-addressed write
                                          (indexes/<dataset_hash>/)

This module is the entirety of the index build. Public entrypoints:

- ``build_index_for_dataset`` — orchestrator. Idempotent w.r.t. ``dataset_hash``.
- ``BuildResult`` — return value of the orchestrator.
- ``assign_eval_units`` — the eval-unit assignment rule, also useful from tests.

``dataset_hash`` is **content-addressed**: same logical data + same schema +
same build args ⇒ same hash. See ``compute_payload_hash`` for the algorithm.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import duckdb
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from socbench._version import __version__
from socbench.config import DatasetEntry, IndexConfig
from socbench.hashing import canonical_json, hash_flow_ids, hash_obj
from socbench.logging_config import get_logger
from socbench.models import EvalUnit
from socbench.schema import CANONICAL_COLUMNS, CanonicalSchema

log = get_logger(__name__)

DEFAULT_FANOUT_K = 10
DEFAULT_WINDOW_MINUTES = 5
MALICIOUS_FRACTION_FOR_MALICIOUS_LABEL = 0.8

# Size bound so a single eval unit stays tractable for an agent operating
# under a finite (effective) context window. A unit exceeding this many flows
# is split into deterministic, contiguous time sub-windows (see
# ``assign_eval_units``). Flows are paginated into the model via tools, so this
# caps how many rows a rendering may have to pull and reason over, plus the
# length of the malicious-flow-id list it must emit. The default 1000 is sized
# to a conservative ~200k-token effective budget at ~90 tokens/flow with ~50%
# reserved for prompt + reasoning + answer. Splitting is by TIME (not by
# destination) so the scope the model perceives — (src_ip[, dst_ip], window) —
# exactly matches the scope its answer is graded against.
DEFAULT_MAX_FLOWS_PER_UNIT = 1000

_PAYLOAD_COLUMNS = [
    "_flow_id",
    "src_ip",
    "dst_ip",
    "ts_start",
    "protocol",
    "src_port",
    "dst_port",
    "bytes_in",
    "bytes_out",
    "pkts_in",
    "pkts_out",
    "tcp_flags",
    "flow_duration_ms",
    "sampling_rate",
    "_is_malicious",
    "_attack_label",
]

_FLOW_ID_SORT_KEYS = [
    "ts_start",
    "src_ip",
    "dst_ip",
    "src_port",
    "dst_port",
    "protocol",
    "bytes_out",
    "pkts_out",
]


# ===========================================================================
# 1. Normalize: parquet → canonical columns + labels
# ===========================================================================


@dataclass(frozen=True)
class NormalizationReport:
    source_columns: list[str]
    column_resolution: dict[str, str | None]
    timestamp_unit_detected: str
    label_columns_used: list[str]
    row_count: int


def _list_source_columns(con: duckdb.DuckDBPyConnection, sources: list[str]) -> list[str]:
    quoted = ", ".join(f"'{s}'" for s in sources)
    rows = con.execute(
        f"SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet([{quoted}]) LIMIT 0)"
    ).fetchall()
    return [r[0] for r in rows]


def _detect_timestamp_unit(
    con: duckdb.DuckDBPyConnection, sources: list[str], src_col: str, schema: CanonicalSchema
) -> str:
    """``'ms'`` or ``'s'`` — alias membership first, numeric magnitude fallback."""
    ms_aliases = {a.lower() for a in schema.timestamp_inference.millisecond_aliases}
    if src_col.lower() in ms_aliases:
        return "ms"
    quoted = ", ".join(f"'{s}'" for s in sources)
    sample = con.execute(
        f'SELECT MEDIAN("{src_col}") FROM read_parquet([{quoted}]) LIMIT 1'
    ).fetchone()
    if sample is None or sample[0] is None:
        return "s"
    return "ms" if float(sample[0]) > 1e12 else "s"


def _resolve_label_columns(
    schema: CanonicalSchema, present_columns: list[str]
) -> tuple[str | None, str | None]:
    rules = schema.label_inference.mixed_dataset_rules
    by_lower = {c.lower(): c for c in present_columns}
    attack_col = next(
        (by_lower[c.lower()] for c in rules.attack_columns if c.lower() in by_lower),
        None,
    )
    label_col = next(
        (by_lower[c.lower()] for c in rules.label_columns if c.lower() in by_lower),
        None,
    )
    return attack_col, label_col


def normalize_parquet(  # noqa: PLR0912, PLR0915 — single linear pipeline; splitting hides the shape
    sources: list[Path],
    *,
    schema: CanonicalSchema,
) -> tuple[pl.DataFrame, NormalizationReport]:
    """Normalize one or more parquet files into the canonical column set."""
    if not sources:
        raise ValueError("normalize_parquet: no source paths provided")
    source_strs = [str(p) for p in sources]

    con = duckdb.connect()
    try:
        present = _list_source_columns(con, source_strs)
        if not present:
            raise ValueError(f"no columns reported by DuckDB for sources={source_strs}")

        resolution: dict[str, str | None] = {
            c: schema.resolve_source_column(c, present) for c in CANONICAL_COLUMNS
        }
        missing_required = [
            c for c in CANONICAL_COLUMNS if resolution[c] is None and c != "tcp_flags"
        ]
        if missing_required:
            raise ValueError(
                f"missing required canonical columns after alias resolution: {missing_required}; "
                f"source had: {present}"
            )

        ts_src = resolution["ts_start"]
        assert ts_src is not None
        ts_unit = _detect_timestamp_unit(con, source_strs, ts_src, schema)
        if ts_unit == "ms":
            ts_expr = f'CAST("{ts_src}" AS DOUBLE) / 1000.0'
        else:
            ts_expr = f'CAST("{ts_src}" AS DOUBLE)'

        attack_col, label_col = _resolve_label_columns(schema, present)

        select_parts: list[str] = []
        for col in CANONICAL_COLUMNS:
            src = resolution[col]
            if col == "ts_start":
                select_parts.append(f"{ts_expr} AS ts_start")
                continue
            if src is None:
                select_parts.append("'' AS tcp_flags")
                continue
            if col in {"src_ip", "dst_ip", "protocol", "tcp_flags"}:
                select_parts.append(f'CAST("{src}" AS VARCHAR) AS {col}')
            elif col in {"src_port", "dst_port", "sampling_rate"}:
                select_parts.append(f'CAST("{src}" AS BIGINT) AS {col}')
            else:
                select_parts.append(f'CAST("{src}" AS DOUBLE) AS {col}')

        if attack_col is not None:
            attack_expr = (
                f'COALESCE(NULLIF(TRIM(CAST("{attack_col}" AS VARCHAR)), \'\'), \'benign\')'
            )
            is_mal_expr = (
                f'CASE WHEN LOWER(COALESCE(CAST("{attack_col}" AS VARCHAR), \'\')) '
                f"IN ('', 'benign') THEN FALSE ELSE TRUE END"
            )
        elif label_col is not None:
            attack_expr = f"CASE WHEN \"{label_col}\" > 0 THEN 'malicious' ELSE 'benign' END"
            is_mal_expr = f'CASE WHEN "{label_col}" > 0 THEN TRUE ELSE FALSE END'
        else:
            attack_expr = "'benign'"
            is_mal_expr = "FALSE"

        select_parts.append(f"{attack_expr} AS _attack_label")
        select_parts.append(f"{is_mal_expr} AS _is_malicious")
        if label_col is not None:
            select_parts.append(f'CAST("{label_col}" AS BIGINT) AS _numeric_label')
        else:
            select_parts.append("CAST(NULL AS BIGINT) AS _numeric_label")

        quoted = ", ".join(f"'{s}'" for s in source_strs)
        query = f"SELECT {', '.join(select_parts)} FROM read_parquet([{quoted}])"
        arrow_table = con.execute(query).to_arrow_table()
    finally:
        con.close()

    df = pl.from_arrow(arrow_table)
    if not isinstance(df, pl.DataFrame):  # pragma: no cover — defensive
        raise TypeError(f"DuckDB → arrow → polars returned {type(df)}")

    df = df.with_columns(
        pl.col("sampling_rate").fill_null(1).cast(pl.UInt32),
        pl.col("tcp_flags").fill_null("").cast(pl.Utf8),
    )

    report = NormalizationReport(
        source_columns=present,
        column_resolution=resolution,
        timestamp_unit_detected=ts_unit,
        label_columns_used=[c for c in (attack_col, label_col) if c is not None],
        row_count=df.height,
    )
    log.info(
        "normalized parquet input",
        extra={
            "sources": source_strs,
            "rows": df.height,
            "timestamp_unit": ts_unit,
            "attack_col": attack_col,
            "label_col": label_col,
        },
    )
    return df, report


# ===========================================================================
# 2. assign_flow_ids: deterministic monotonic ids
# ===========================================================================


def assign_flow_ids(df: pl.DataFrame) -> pl.DataFrame:
    """Sort ``df`` deterministically and prepend a ``_flow_id`` column (UInt64)."""
    missing = [c for c in _FLOW_ID_SORT_KEYS if c not in df.columns]
    if missing:
        raise ValueError(f"assign_flow_ids: missing sort keys {missing}")
    df = df.sort(by=_FLOW_ID_SORT_KEYS, descending=False, maintain_order=False)
    return df.with_row_index(name="_flow_id").with_columns(
        pl.col("_flow_id").cast(pl.UInt64)
    )


# ===========================================================================
# 3. Derivations: pairs, hosts, rollups
# ===========================================================================


def _pair_id_for(src_ip: str, dst_ip: str) -> str:
    return hash_obj([src_ip, dst_ip])[:16]


def derive_pairs(flows: pl.DataFrame) -> pl.DataFrame:
    """Per ``(src_ip, dst_ip)`` aggregate with a stable ``pair_id``."""
    grouped = (
        flows.group_by(["src_ip", "dst_ip"], maintain_order=False)
        .agg(
            flow_count=pl.len(),
            malicious_flow_count=pl.col("_is_malicious").sum(),
            ts_start_min=pl.col("ts_start").min(),
            ts_start_max=pl.col("ts_start").max(),
            bytes_total=(pl.col("bytes_in") + pl.col("bytes_out")).sum(),
            pkts_total=(pl.col("pkts_in") + pl.col("pkts_out")).sum(),
            distinct_dst_ports=pl.col("dst_port").n_unique(),
            distinct_src_ports=pl.col("src_port").n_unique(),
        )
        .sort(["src_ip", "dst_ip"])
    )
    pair_ids = [
        _pair_id_for(s, d) for s, d in zip(grouped["src_ip"], grouped["dst_ip"], strict=True)
    ]
    return grouped.with_columns(pl.Series(name="pair_id", values=pair_ids)).select(
        "pair_id",
        "src_ip",
        "dst_ip",
        pl.col("flow_count").cast(pl.UInt64),
        pl.col("malicious_flow_count").cast(pl.UInt64),
        "ts_start_min",
        "ts_start_max",
        pl.col("bytes_total").cast(pl.Float64),
        pl.col("pkts_total").cast(pl.Float64),
        pl.col("distinct_dst_ports").cast(pl.UInt32),
        pl.col("distinct_src_ports").cast(pl.UInt32),
    )


def derive_hosts(flows: pl.DataFrame) -> pl.DataFrame:
    """Per-``src_ip`` aggregate."""
    return (
        flows.group_by("src_ip", maintain_order=False)
        .agg(
            flow_count=pl.len(),
            malicious_flow_count=pl.col("_is_malicious").sum(),
            distinct_destinations=pl.col("dst_ip").n_unique(),
            distinct_malicious_destinations=pl.col("dst_ip")
            .filter(pl.col("_is_malicious"))
            .n_unique(),
            ts_start_min=pl.col("ts_start").min(),
            ts_start_max=pl.col("ts_start").max(),
            bytes_out_total=pl.col("bytes_out").sum(),
            bytes_in_total=pl.col("bytes_in").sum(),
        )
        .rename({"src_ip": "host"})
        .sort("host")
        .select(
            "host",
            pl.col("flow_count").cast(pl.UInt64),
            pl.col("malicious_flow_count").cast(pl.UInt64),
            pl.col("distinct_destinations").cast(pl.UInt64),
            pl.col("distinct_malicious_destinations").cast(pl.UInt64),
            "ts_start_min",
            "ts_start_max",
            pl.col("bytes_out_total").cast(pl.Float64),
            pl.col("bytes_in_total").cast(pl.Float64),
        )
    )


def hosts_rollup(flows: pl.DataFrame) -> pl.DataFrame:
    """Per-``src_ip`` rollup written to ``rollups/hosts.parquet``."""
    return (
        flows.group_by("src_ip", maintain_order=False)
        .agg(
            flow_count=pl.len(),
            malicious_flow_count=pl.col("_is_malicious").sum(),
            distinct_destinations=pl.col("dst_ip").n_unique(),
            bytes_out_total=pl.col("bytes_out").sum(),
            bytes_in_total=pl.col("bytes_in").sum(),
            pkts_out_total=pl.col("pkts_out").sum(),
            pkts_in_total=pl.col("pkts_in").sum(),
            ts_start_min=pl.col("ts_start").min(),
            ts_start_max=pl.col("ts_start").max(),
            distinct_dst_ports=pl.col("dst_port").n_unique(),
            distinct_protocols=pl.col("protocol").n_unique(),
        )
        .rename({"src_ip": "host"})
        .sort("host")
    )


def pair_stats_rollup(flows: pl.DataFrame) -> pl.DataFrame:
    """Per-pair rollup written to ``rollups/pair_stats.parquet``."""
    return (
        flows.group_by(["src_ip", "dst_ip"], maintain_order=False)
        .agg(
            flow_count=pl.len(),
            malicious_flow_count=pl.col("_is_malicious").sum(),
            bytes_total=(pl.col("bytes_in") + pl.col("bytes_out")).sum(),
            pkts_total=(pl.col("pkts_in") + pl.col("pkts_out")).sum(),
            distinct_dst_ports=pl.col("dst_port").n_unique(),
            distinct_src_ports=pl.col("src_port").n_unique(),
            distinct_protocols=pl.col("protocol").n_unique(),
            ts_start_min=pl.col("ts_start").min(),
            ts_start_max=pl.col("ts_start").max(),
            bytes_in_total=pl.col("bytes_in").sum(),
            bytes_out_total=pl.col("bytes_out").sum(),
        )
        .sort(["src_ip", "dst_ip"])
    )


# ===========================================================================
# 4. Eval-unit assignment
# ===========================================================================


@dataclass(frozen=True)
class EvalUnitAssignmentReport:
    pair_timeline_count: int
    host_egress_count: int
    benign_units: int
    mixed_units: int
    malicious_units: int
    host_egress_hosts: int


def _gold_label(mal_count: int, total_count: int) -> str:
    if mal_count == 0:
        return "benign"
    if mal_count >= MALICIOUS_FRACTION_FOR_MALICIOUS_LABEL * total_count:
        return "malicious"
    return "mixed"


def _chunk_bounds(n: int, size: int) -> list[tuple[int, int]]:
    """Contiguous ``[start, end)`` ranges of width ``size`` covering ``[0, n)``.

    ``size <= 0`` means "no split" (a single range). Used to break an
    ordered flow list into time-contiguous sub-windows.
    """
    if n <= 0:
        return []
    if size <= 0 or n <= size:
        return [(0, n)]
    return [(a, min(a + size, n)) for a in range(0, n, size)]


def assign_eval_units(
    flows: pl.DataFrame,
    *,
    fanout_K: int = DEFAULT_FANOUT_K,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    max_flows_per_unit: int = DEFAULT_MAX_FLOWS_PER_UNIT,
) -> tuple[list[EvalUnit], EvalUnitAssignmentReport]:
    """Eval-unit assignment rule. See module docstring for the operationalization.

    A host is in ``host_egress`` mode iff its **maximum** distinct destination
    IPs (regardless of label) in any ``window_minutes`` bucket reaches ``K``.
    The promotion decision is deliberately **label-agnostic**: a real pipeline
    cannot know which destinations are malicious when it scopes a host, so the
    unit boundaries must not be derived from ground truth. This also yields
    genuine benign fan-out units (legit wide-fan-out hosts), which are an
    important false-positive trap. ``gold_label`` is still computed afterward
    from the malicious fraction.

    **Size bounding (by time).** No single unit may exceed ``max_flows_per_unit``
    flows, so the agent stays within a finite context window. Oversized units are
    split into contiguous, time-ordered sub-windows — never by destination —
    because the scope the model perceives from the kickoff is
    ``(src_ip[, dst_ip], time_window)``; a time split keeps that perceived scope
    identical to the scope the answer is graded against, whereas a destination
    split would leave two sub-units sharing the same src_ip and window and thus be
    ill-posed. Tools remain corpus-global, so splitting bounds only the *grading*
    scope, never the model's ability to cross-search. ``pair_timeline`` pairs and
    ``host_egress`` buckets are both chunked over flows ordered by
    (``ts_start``, ``_flow_id``); ``gold_label`` and ``distinct_destinations`` are
    recomputed per sub-unit. Splitting is deterministic so ``eval_unit_id`` hashes
    are stable.
    """
    if "_flow_id" not in flows.columns:
        raise ValueError("assign_eval_units: input frame is missing `_flow_id`")
    if "_is_malicious" not in flows.columns:
        raise ValueError("assign_eval_units: input frame is missing `_is_malicious`")

    window_seconds = window_minutes * 60
    annotated = flows.with_columns(
        bucket=(pl.col("ts_start") / window_seconds).floor().cast(pl.Int64),
    )

    # Classify each src_ip: max-over-buckets of distinct destination IPs.
    # Label-agnostic on purpose — unit boundaries must not depend on ground
    # truth (see the function docstring).
    per_host_bucket = (
        annotated.group_by(["src_ip", "bucket"], maintain_order=False)
        .agg(distinct_dst=pl.col("dst_ip").n_unique())
    )
    if per_host_bucket.is_empty():
        host_egress_hosts: set[str] = set()
    else:
        host_max = per_host_bucket.group_by("src_ip", maintain_order=False).agg(
            max_distinct_dst=pl.col("distinct_dst").max()
        )
        host_egress_hosts = set(
            host_max.filter(pl.col("max_distinct_dst") >= fanout_K)["src_ip"].to_list()
        )

    units: list[EvalUnit] = []

    # pair_timeline mode — one unit per (src_ip, dst_ip), split into contiguous
    # time chunks of at most ``max_flows_per_unit`` flows.
    pair_mode = annotated.filter(~pl.col("src_ip").is_in(list(host_egress_hosts)))
    if not pair_mode.is_empty():
        per_pair = (
            pair_mode.sort(["src_ip", "dst_ip", "ts_start", "_flow_id"])
            .group_by(["src_ip", "dst_ip"], maintain_order=True)
            .agg(
                flow_ids=pl.col("_flow_id"),
                is_mal=pl.col("_is_malicious").cast(pl.Int64),
                tss=pl.col("ts_start"),
            )
        )
        for row in per_pair.iter_rows(named=True):
            fids = [int(x) for x in row["flow_ids"]]
            ismal = [int(x) for x in row["is_mal"]]
            tss = [float(x) for x in row["tss"]]
            for a, b in _chunk_bounds(len(fids), max_flows_per_unit):
                cf = fids[a:b]
                mal = sum(ismal[a:b])
                units.append(
                    EvalUnit(
                        eval_unit_id="pt-" + hash_flow_ids(cf)[:16],
                        unit_type="pair_timeline",
                        src_ip=row["src_ip"],
                        dst_ip=row["dst_ip"],
                        flow_ids=cf,
                        flow_count=len(cf),
                        malicious_flow_count=mal,
                        gold_label=_gold_label(mal, len(cf)),  # type: ignore[arg-type]
                        ts_start_min=min(tss[a:b]),
                        ts_start_max=max(tss[a:b]),
                        distinct_destinations=1,
                    )
                )

    # host_egress mode — one unit per (src_ip, bucket), split into contiguous
    # time chunks of at most ``max_flows_per_unit`` flows. distinct_destinations
    # is recomputed per chunk.
    host_mode = annotated.filter(pl.col("src_ip").is_in(list(host_egress_hosts)))
    if not host_mode.is_empty():
        per_window = (
            host_mode.sort(["src_ip", "bucket", "ts_start", "_flow_id"])
            .group_by(["src_ip", "bucket"], maintain_order=True)
            .agg(
                flow_ids=pl.col("_flow_id"),
                is_mal=pl.col("_is_malicious").cast(pl.Int64),
                tss=pl.col("ts_start"),
                dsts=pl.col("dst_ip"),
            )
        )
        for row in per_window.iter_rows(named=True):
            fids = [int(x) for x in row["flow_ids"]]
            ismal = [int(x) for x in row["is_mal"]]
            tss = [float(x) for x in row["tss"]]
            dsts = list(row["dsts"])
            bucket = int(row["bucket"])
            for a, b in _chunk_bounds(len(fids), max_flows_per_unit):
                cf = fids[a:b]
                mal = sum(ismal[a:b])
                units.append(
                    EvalUnit(
                        eval_unit_id="he-" + hash_obj([row["src_ip"], bucket, cf])[:16],
                        unit_type="host_egress",
                        src_ip=row["src_ip"],
                        dst_ip=None,
                        flow_ids=cf,
                        flow_count=len(cf),
                        malicious_flow_count=mal,
                        gold_label=_gold_label(mal, len(cf)),  # type: ignore[arg-type]
                        ts_start_min=min(tss[a:b]),
                        ts_start_max=max(tss[a:b]),
                        distinct_destinations=len(set(dsts[a:b])),
                    )
                )

    units.sort(key=lambda u: (u.unit_type, u.src_ip, u.dst_ip or "", u.ts_start_min))

    report = EvalUnitAssignmentReport(
        pair_timeline_count=sum(1 for u in units if u.unit_type == "pair_timeline"),
        host_egress_count=sum(1 for u in units if u.unit_type == "host_egress"),
        benign_units=sum(1 for u in units if u.gold_label == "benign"),
        mixed_units=sum(1 for u in units if u.gold_label == "mixed"),
        malicious_units=sum(1 for u in units if u.gold_label == "malicious"),
        host_egress_hosts=len(host_egress_hosts),
    )
    log.info(
        "assigned eval units",
        extra={
            "fanout_K": fanout_K,
            "window_minutes": window_minutes,
            "max_flows_per_unit": max_flows_per_unit,
            **report.__dict__,
        },
    )
    return units, report


# ===========================================================================
# 5. Manifest + dataset_hash
# ===========================================================================


class IndexManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_hash: str
    dataset_name: str
    schema_hash: str
    payload_hash: str
    flow_count: Annotated[int, Field(ge=0)]
    pair_count: Annotated[int, Field(ge=0)]
    host_count: Annotated[int, Field(ge=0)]
    eval_unit_count: Annotated[int, Field(ge=0)]
    build_args: dict[str, Any]
    source_paths: list[str]
    built_at_utc: str
    socbench_version: str


def compute_payload_hash(flows: pl.DataFrame) -> str:
    """Stable hash of ``flows.parquet`` content (excludes parquet container bytes).

    Iterates in chunks so the full row-serialized payload never sits in memory.
    """
    missing = [c for c in _PAYLOAD_COLUMNS if c not in flows.columns]
    if missing:
        raise ValueError(f"compute_payload_hash: missing columns {missing}")
    sorted_df = flows.sort("_flow_id").select(_PAYLOAD_COLUMNS)
    h = hashlib.blake2b(digest_size=16)
    for batch in sorted_df.iter_slices(n_rows=10_000):
        for row in batch.iter_rows(named=False):
            h.update(canonical_json(list(row)))
    return h.hexdigest()


def compute_dataset_hash(
    *, schema_hash: str, payload_hash: str, build_args: dict[str, Any]
) -> str:
    return hash_obj(
        {"schema_hash": schema_hash, "build_args": build_args, "payload_hash": payload_hash}
    )


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# 6. Store: write the on-disk artifact tree
# ===========================================================================


def index_dir_for(index_root: Path, dataset_hash: str) -> Path:
    return index_root / dataset_hash


def is_index_complete(index_dir: Path) -> bool:
    required = [
        "manifest.json",
        "flows.parquet",
        "pairs.jsonl",
        "hosts.jsonl",
        "eval_units.jsonl",
        "rollups/hosts.parquet",
        "rollups/pair_stats.parquet",
    ]
    return all((index_dir / name).exists() for name in required)


def _write_jsonl(path: Path, df: pl.DataFrame) -> None:
    with open(path, "wb") as fh:
        for row in df.iter_rows(named=True):
            fh.write(canonical_json(row))
            fh.write(b"\n")


def _write_index(
    *,
    index_dir: Path,
    flows: pl.DataFrame,
    pairs: pl.DataFrame,
    hosts: pl.DataFrame,
    eval_units: list[EvalUnit],
    hosts_rollup_df: pl.DataFrame,
    pair_stats_rollup_df: pl.DataFrame,
    manifest: IndexManifest,
) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "rollups").mkdir(parents=True, exist_ok=True)

    flows_path = index_dir / "flows.parquet"
    flows.write_parquet(flows_path, compression="zstd", statistics=True)
    log.info("wrote flows.parquet", extra={"rows": flows.height, "path": str(flows_path)})

    _write_jsonl(index_dir / "pairs.jsonl", pairs)
    _write_jsonl(index_dir / "hosts.jsonl", hosts)

    eval_units_path = index_dir / "eval_units.jsonl"
    with open(eval_units_path, "wb") as fh:
        for unit in eval_units:
            fh.write(canonical_json(unit.model_dump()))
            fh.write(b"\n")
    log.info(
        "wrote eval_units.jsonl",
        extra={"units": len(eval_units), "path": str(eval_units_path)},
    )

    hosts_rollup_df.write_parquet(
        index_dir / "rollups" / "hosts.parquet", compression="zstd", statistics=True
    )
    pair_stats_rollup_df.write_parquet(
        index_dir / "rollups" / "pair_stats.parquet", compression="zstd", statistics=True
    )

    (index_dir / "manifest.json").write_bytes(canonical_json(manifest.model_dump()))
    log.info("wrote index", extra={"index_dir": str(index_dir)})


# ===========================================================================
# 7. Orchestrator
# ===========================================================================


@dataclass(frozen=True)
class BuildResult:
    dataset_hash: str
    index_dir: Path
    flow_count: int
    pair_count: int
    host_count: int
    eval_unit_count: int
    was_rebuilt: bool


def build_index_for_dataset(
    *,
    dataset_name: str,
    dataset: DatasetEntry,
    schema: CanonicalSchema,
    index_cfg: IndexConfig,
    index_root: Path,
    rebuild: bool = False,
) -> BuildResult:
    """Build a content-addressed index for one dataset entry. Idempotent."""
    flows_raw, _ = normalize_parquet(dataset.paths, schema=schema)
    flows = assign_flow_ids(flows_raw)

    payload_hash = compute_payload_hash(flows)
    build_args = {
        "dataset_name": dataset_name,
        "host_egress_fanout_K": index_cfg.host_egress_fanout_K,
        "host_egress_window_minutes": index_cfg.host_egress_window_minutes,
        "max_flows_per_unit": index_cfg.max_flows_per_unit,
    }
    dataset_hash = compute_dataset_hash(
        schema_hash=schema.schema_hash, payload_hash=payload_hash, build_args=build_args
    )
    target_dir = index_dir_for(index_root, dataset_hash)

    if is_index_complete(target_dir) and not rebuild:
        log.info(
            "index already exists — skipping (pass --rebuild to force)",
            extra={"dataset_hash": dataset_hash, "index_dir": str(target_dir)},
        )
        manifest = IndexManifest.model_validate(
            json.loads((target_dir / "manifest.json").read_text("utf-8"))
        )
        return BuildResult(
            dataset_hash=dataset_hash,
            index_dir=target_dir,
            flow_count=manifest.flow_count,
            pair_count=manifest.pair_count,
            host_count=manifest.host_count,
            eval_unit_count=manifest.eval_unit_count,
            was_rebuilt=False,
        )

    pairs = derive_pairs(flows)
    hosts = derive_hosts(flows)
    units, _ = assign_eval_units(
        flows,
        fanout_K=index_cfg.host_egress_fanout_K,
        window_minutes=index_cfg.host_egress_window_minutes,
        max_flows_per_unit=index_cfg.max_flows_per_unit,
    )
    hosts_roll = hosts_rollup(flows)
    pair_stats_roll = pair_stats_rollup(flows)

    manifest = IndexManifest(
        dataset_hash=dataset_hash,
        dataset_name=dataset_name,
        schema_hash=schema.schema_hash,
        payload_hash=payload_hash,
        flow_count=flows.height,
        pair_count=pairs.height,
        host_count=hosts.height,
        eval_unit_count=len(units),
        build_args=build_args,
        source_paths=[str(p) for p in dataset.paths],
        built_at_utc=_utc_now_iso(),
        socbench_version=__version__,
    )
    _write_index(
        index_dir=target_dir,
        flows=flows,
        pairs=pairs,
        hosts=hosts,
        eval_units=units,
        hosts_rollup_df=hosts_roll,
        pair_stats_rollup_df=pair_stats_roll,
        manifest=manifest,
    )

    return BuildResult(
        dataset_hash=dataset_hash,
        index_dir=target_dir,
        flow_count=flows.height,
        pair_count=pairs.height,
        host_count=hosts.height,
        eval_unit_count=len(units),
        was_rebuilt=True,
    )


__all__ = [
    "DEFAULT_FANOUT_K",
    "DEFAULT_MAX_FLOWS_PER_UNIT",
    "DEFAULT_WINDOW_MINUTES",
    "BuildResult",
    "EvalUnitAssignmentReport",
    "IndexManifest",
    "NormalizationReport",
    "assign_eval_units",
    "assign_flow_ids",
    "build_index_for_dataset",
    "compute_dataset_hash",
    "compute_payload_hash",
    "derive_hosts",
    "derive_pairs",
    "hosts_rollup",
    "index_dir_for",
    "is_index_complete",
    "normalize_parquet",
    "pair_stats_rollup",
]

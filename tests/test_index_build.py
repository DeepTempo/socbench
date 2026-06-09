"""End-to-end index build: determinism, manifest stability, eval-unit assignment."""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from socbench.config import DatasetEntry, IndexConfig
from socbench.index import (
    DEFAULT_FANOUT_K,
    DEFAULT_WINDOW_MINUTES,
    assign_eval_units,
    assign_flow_ids,
    build_index_for_dataset,
    is_index_complete,
    normalize_parquet,
)


def test_normalize_resolves_aliases(synthetic_parquet: Path, schema) -> None:
    df, report = normalize_parquet([synthetic_parquet], schema=schema)
    assert df.height > 0
    assert report.row_count == df.height
    # The synthetic generator wrote canonical-named columns, so resolution should be canonical.
    for col in ("src_ip", "dst_ip", "ts_start", "protocol"):
        assert report.column_resolution[col] is not None
    assert "_is_malicious" in df.columns
    assert "_attack_label" in df.columns


def test_flow_ids_monotonic_and_sorted(synthetic_parquet: Path, schema) -> None:
    df, _ = normalize_parquet([synthetic_parquet], schema=schema)
    df = assign_flow_ids(df)
    assert df["_flow_id"].is_sorted()
    assert df["_flow_id"].to_list() == list(range(df.height))


def test_eval_unit_assignment_produces_both_unit_types(synthetic_parquet: Path, schema) -> None:
    df, _ = normalize_parquet([synthetic_parquet], schema=schema)
    df = assign_flow_ids(df)
    units, report = assign_eval_units(df)
    assert report.host_egress_count >= 1, "scanner host should trigger host_egress mode"
    assert (
        report.pair_timeline_count >= 1
    ), "benign + single-target attack pairs should yield pair_timeline units"
    # 10.0.99.99 is the scanner host
    assert any(u.unit_type == "host_egress" and u.src_ip == "10.0.99.99" for u in units)
    # Benign pair examples should be pair_timeline with gold_label benign
    assert any(u.unit_type == "pair_timeline" and u.gold_label == "benign" for u in units)
    # Single-target attack pair should be pair_timeline with gold_label malicious
    assert any(u.unit_type == "pair_timeline" and u.gold_label == "malicious" for u in units)


def test_eval_unit_K_threshold_is_configurable(synthetic_parquet: Path, schema) -> None:
    df, _ = normalize_parquet([synthetic_parquet], schema=schema)
    df = assign_flow_ids(df)

    # With K bumped above the synthetic fan-out (12), nothing should be host_egress.
    _, report_high_K = assign_eval_units(df, fanout_K=100, window_minutes=DEFAULT_WINDOW_MINUTES)
    assert report_high_K.host_egress_count == 0

    # Default K=10 should produce host_egress for the scanner host.
    _, report_default = assign_eval_units(df)
    assert report_default.host_egress_count >= 1
    assert DEFAULT_FANOUT_K == 10  # canary on the documented default


def test_index_build_is_idempotent_and_deterministic(
    synthetic_parquet: Path, tmp_path: Path, schema
) -> None:
    index_root = tmp_path / "indexes"
    dataset = DatasetEntry(paths=[synthetic_parquet], label_group="malicious")

    r1 = build_index_for_dataset(
        dataset_name="synth",
        dataset=dataset,
        schema=schema,
        index_cfg=IndexConfig(),
        index_root=index_root,
    )
    assert r1.was_rebuilt is True
    assert is_index_complete(r1.index_dir)

    r2 = build_index_for_dataset(
        dataset_name="synth",
        dataset=dataset,
        schema=schema,
        index_cfg=IndexConfig(),
        index_root=index_root,
    )
    assert r2.was_rebuilt is False  # idempotent
    assert r2.dataset_hash == r1.dataset_hash

    # The manifest's payload_hash matches a fresh recomputation.
    manifest = json.loads((r2.index_dir / "manifest.json").read_text("utf-8"))
    assert manifest["dataset_hash"] == r2.dataset_hash

    # Forcing a rebuild should produce the same dataset_hash (true content-addressing).
    r3 = build_index_for_dataset(
        dataset_name="synth",
        dataset=dataset,
        schema=schema,
        index_cfg=IndexConfig(),
        index_root=index_root,
        rebuild=True,
    )
    assert r3.dataset_hash == r1.dataset_hash


def test_flows_parquet_is_readable(built_index) -> None:  # type: ignore[no-untyped-def]
    flows = pl.read_parquet(built_index.index_dir / "flows.parquet")
    assert flows.height == built_index.flow_count
    assert "_flow_id" in flows.columns
    assert "_is_malicious" in flows.columns


def test_changing_K_changes_dataset_hash(synthetic_parquet: Path, tmp_path: Path, schema) -> None:
    """Different build args MUST produce different dataset_hash values."""
    index_root = tmp_path / "indexes"
    dataset = DatasetEntry(paths=[synthetic_parquet], label_group="malicious")

    r_K10 = build_index_for_dataset(
        dataset_name="synth",
        dataset=dataset,
        schema=schema,
        index_cfg=IndexConfig(host_egress_fanout_K=10),
        index_root=index_root,
    )
    r_K20 = build_index_for_dataset(
        dataset_name="synth",
        dataset=dataset,
        schema=schema,
        index_cfg=IndexConfig(host_egress_fanout_K=20),
        index_root=index_root,
    )
    assert r_K10.dataset_hash != r_K20.dataset_hash

"""Deterministic synthetic NetFlow generator (test-only).

Generates a small parquet that exercises both the ``pair_timeline`` and
``host_egress`` assignment paths so the eval-unit tests have non-trivial
coverage.

This module is intentionally NOT importable from the installed package — it
lives under ``tests/`` and is only used by pytest.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import polars as pl


@dataclass(frozen=True)
class SyntheticFlowSpec:
    n_benign_pairs: int = 8
    benign_flows_per_pair: int = 5
    n_singletarget_attack_pairs: int = 2  # forces pair_timeline
    attacker_pair_flows: int = 4
    scanner_host: str = "10.0.99.99"
    scanner_dst_count: int = 12  # ≥ default K=10 → host_egress mode
    scanner_flows_per_dst: int = 2
    base_ts: float = 1_700_000_000.0
    seed: int = 7


def generate(spec: SyntheticFlowSpec = SyntheticFlowSpec()) -> pl.DataFrame:
    """Return a polars frame with raw (un-aliased) canonical column names."""
    rng = random.Random(spec.seed)
    rows: list[dict[str, object]] = []
    ts = spec.base_ts

    for pair_i in range(spec.n_benign_pairs):
        src = f"10.0.0.{pair_i + 1}"
        dst = f"10.0.1.{pair_i + 1}"
        for _ in range(spec.benign_flows_per_pair):
            rows.append(_flow(src, dst, ts, rng, attack="benign"))
            ts += 1.0

    for pair_i in range(spec.n_singletarget_attack_pairs):
        src = f"10.0.10.{pair_i + 1}"
        dst = f"10.0.20.{pair_i + 1}"
        for _ in range(spec.attacker_pair_flows):
            rows.append(_flow(src, dst, ts, rng, attack="Brute Force"))
            ts += 1.0

    # Scanner host: many distinct destinations in a short window → host_egress
    scan_start = ts
    for d_i in range(spec.scanner_dst_count):
        dst = f"10.0.30.{d_i + 1}"
        for _ in range(spec.scanner_flows_per_dst):
            rows.append(_flow(spec.scanner_host, dst, ts, rng, attack="PortScan"))
            ts += 0.5  # all scanner flows within a 1-minute window
    # Sanity: total time span < window_minutes * 60
    assert ts - scan_start < 5 * 60, "scanner block must fit in default 5 min window"

    return pl.DataFrame(rows)


def _flow(src: str, dst: str, ts: float, rng: random.Random, *, attack: str) -> dict[str, object]:
    is_tcp = rng.random() < 0.7
    return {
        "src_ip": src,
        "dst_ip": dst,
        "ts_start": ts,
        "protocol": "TCP" if is_tcp else "UDP",
        "src_port": rng.randint(1024, 60000),
        "dst_port": rng.choice([80, 443, 22, 53, 8080, 9999]),
        "bytes_in": float(rng.randint(80, 5000)),
        "bytes_out": float(rng.randint(80, 5000)),
        "pkts_in": float(rng.randint(1, 20)),
        "pkts_out": float(rng.randint(1, 20)),
        "tcp_flags": "S" if is_tcp else "",
        "flow_duration_ms": float(rng.randint(1, 60_000)),
        "sampling_rate": 1,
        "Attack": attack,
        "Label": 0 if attack == "benign" else 1,
    }


def write_synthetic_parquet(path: Path, spec: SyntheticFlowSpec = SyntheticFlowSpec()) -> Path:
    df = generate(spec)
    df.write_parquet(path, compression="zstd", statistics=True)
    return path

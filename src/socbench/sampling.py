"""Step 6 — stratified sampling.

Strata are ``(unit_type, gold_label)`` → up to six buckets
(``{pair_timeline, host_egress} × {benign, malicious, mixed}``). The
selection is deterministic in ``(dataset_hash, sample_seed, mode)``: the
same triple always yields the same eval-unit set, which is what lets
the single-shot baseline, the main agent run, and every ablation score the
*same* units.

Modes (defaults from ``benchmark_config.yaml``):

- **smoke** — 1 unit per non-empty stratum, topped up to ``min_total_units``
  (default 8) from the leftover pool if the strata alone don't reach it.
- **full**  — ``units_per_stratum`` (default 10) per stratum, then the whole
  selection is capped at ``full_unit_cap`` (default 60).

Empty strata are reported as ``stratum_undersampled`` — never an error.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from itertools import zip_longest

from socbench.config import SamplingModeConfig
from socbench.hashing import hash_obj
from socbench.models import EvalUnit, RunMode

# All six strata, in a fixed order so the report is stable.
_UNIT_TYPES = ("pair_timeline", "host_egress")
_GOLD_LABELS = ("benign", "malicious", "mixed")
ALL_STRATA: tuple[tuple[str, str], ...] = tuple(
    (ut, gl) for ut in _UNIT_TYPES for gl in _GOLD_LABELS
)


@dataclass(frozen=True)
class SampleReport:
    mode: RunMode
    sample_seed: int
    dataset_hash: str
    total_selected: int
    per_stratum_available: dict[str, int]
    per_stratum_selected: dict[str, int]
    undersampled_strata: list[str] = field(default_factory=list)
    capped: bool = False


@dataclass(frozen=True)
class SampleResult:
    selected: list[EvalUnit]
    report: SampleReport


def _stratum_key(unit: EvalUnit) -> str:
    return f"{unit.unit_type}:{unit.gold_label}"


def _seed_int(*, dataset_hash: str, sample_seed: int, mode: str) -> int:
    """Derive a stable 64-bit RNG seed from the reproducibility triple."""
    digest = hash_obj({"dataset_hash": dataset_hash, "sample_seed": sample_seed, "mode": mode})
    return int(digest[:16], 16)


def stratified_sample(
    units: list[EvalUnit],
    *,
    mode: RunMode,
    sample_seed: int,
    dataset_hash: str,
    mode_cfg: SamplingModeConfig,
) -> SampleResult:
    """Select eval units by stratified sampling. Pure and deterministic.

    The RNG is seeded from ``(dataset_hash, sample_seed, mode)`` so the
    selection is reproducible and shared across the baseline/ablations on the same
    index.
    """
    rng = random.Random(_seed_int(dataset_hash=dataset_hash, sample_seed=sample_seed, mode=mode))

    # Bucket units by stratum. Sort within each bucket by eval_unit_id first so
    # the seeded shuffle is the *only* source of order — independent of the
    # order units arrived in.
    buckets: dict[str, list[EvalUnit]] = {}
    for unit in units:
        buckets.setdefault(_stratum_key(unit), []).append(unit)
    for bucket in buckets.values():
        bucket.sort(key=lambda u: u.eval_unit_id)

    per_stratum_available = {
        f"{ut}:{gl}": len(buckets.get(f"{ut}:{gl}", [])) for (ut, gl) in ALL_STRATA
    }

    selected: list[EvalUnit] = []
    selected_ids: set[str] = set()
    per_stratum_selected: dict[str, int] = {f"{ut}:{gl}": 0 for (ut, gl) in ALL_STRATA}
    undersampled: list[str] = []

    take_per_stratum = mode_cfg.units_per_stratum
    for ut, gl in ALL_STRATA:
        key = f"{ut}:{gl}"
        bucket = list(buckets.get(key, []))
        if not bucket:
            undersampled.append(key)
            continue
        rng.shuffle(bucket)
        chosen = bucket[:take_per_stratum]
        if len(bucket) < take_per_stratum:
            undersampled.append(key)
        for u in chosen:
            selected.append(u)
            selected_ids.add(u.eval_unit_id)
            per_stratum_selected[key] += 1

    # smoke: top up to min_total_units from the leftover pool.
    if mode_cfg.min_total_units is not None and len(selected) < mode_cfg.min_total_units:
        leftover = [u for u in units if u.eval_unit_id not in selected_ids]
        leftover.sort(key=lambda u: u.eval_unit_id)
        rng.shuffle(leftover)
        need = mode_cfg.min_total_units - len(selected)
        for u in leftover[:need]:
            selected.append(u)
            selected_ids.add(u.eval_unit_id)
            per_stratum_selected[_stratum_key(u)] += 1

    # full: cap the whole selection at full_unit_cap.
    capped = False
    if mode_cfg.full_unit_cap is not None and len(selected) > mode_cfg.full_unit_cap:
        rng.shuffle(selected)
        selected = selected[: mode_cfg.full_unit_cap]
        capped = True
        # Recompute per-stratum counts after the cap trim.
        per_stratum_selected = {f"{ut}:{gl}": 0 for (ut, gl) in ALL_STRATA}
        for u in selected:
            per_stratum_selected[_stratum_key(u)] += 1

    # Budget-robust output order: round-robin across strata (one unit per
    # stratum in rotation) so that ANY prefix of the list stays balanced. This
    # matters when a run is truncated mid-way by a cost cap — the Runner is
    # unit-outer, so a stratum-grouped order would finish one stratum before
    # touching the next. Deterministic: each stratum's units keep their seeded
    # selection order; strata rotate in the fixed ALL_STRATA order.
    by_stratum: dict[str, list[EvalUnit]] = {}
    for u in selected:
        by_stratum.setdefault(_stratum_key(u), []).append(u)
    stratum_lists = [
        by_stratum[f"{ut}:{gl}"]
        for (ut, gl) in ALL_STRATA
        if f"{ut}:{gl}" in by_stratum
    ]
    selected = [
        u
        for rotation in zip_longest(*stratum_lists)
        for u in rotation
        if u is not None
    ]

    report = SampleReport(
        mode=mode,
        sample_seed=sample_seed,
        dataset_hash=dataset_hash,
        total_selected=len(selected),
        per_stratum_available=per_stratum_available,
        per_stratum_selected=per_stratum_selected,
        undersampled_strata=undersampled,
        capped=capped,
    )
    return SampleResult(selected=selected, report=report)


__all__ = [
    "ALL_STRATA",
    "SampleReport",
    "SampleResult",
    "stratified_sample",
]

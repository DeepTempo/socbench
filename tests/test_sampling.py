"""Step 6 stratified-sampling tests."""
from __future__ import annotations

from socbench.config import SamplingModeConfig
from socbench.models import EvalUnit
from socbench.sampling import ALL_STRATA, stratified_sample

DATASET_HASH = "abc123"


def _unit(idx: int, unit_type: str, gold_label: str) -> EvalUnit:
    if unit_type == "pair_timeline":
        return EvalUnit(
            eval_unit_id=f"pt-{gold_label}-{idx:03d}",
            unit_type="pair_timeline",
            src_ip=f"10.0.0.{idx}",
            dst_ip=f"10.0.1.{idx}",
            flow_ids=[idx],
            flow_count=1,
            malicious_flow_count=1 if gold_label != "benign" else 0,
            gold_label=gold_label,  # type: ignore[arg-type]
            ts_start_min=0.0,
            ts_start_max=1.0,
            distinct_destinations=1,
        )
    return EvalUnit(
        eval_unit_id=f"he-{gold_label}-{idx:03d}",
        unit_type="host_egress",
        src_ip=f"10.0.0.{idx}",
        dst_ip=None,
        flow_ids=[idx],
        flow_count=1,
        malicious_flow_count=1 if gold_label != "benign" else 0,
        gold_label=gold_label,  # type: ignore[arg-type]
        ts_start_min=0.0,
        ts_start_max=1.0,
        distinct_destinations=3,
    )


def _population(per_stratum: int = 5) -> list[EvalUnit]:
    units: list[EvalUnit] = []
    counter = 0
    for unit_type, gold_label in ALL_STRATA:
        for _ in range(per_stratum):
            units.append(_unit(counter, unit_type, gold_label))
            counter += 1
    return units


SMOKE = SamplingModeConfig(units_per_stratum=1, min_total_units=8, cost_budget_usd=10)
FULL = SamplingModeConfig(units_per_stratum=3, full_unit_cap=10, cost_budget_usd=500)


def test_smoke_takes_one_per_stratum_min_total():
    units = _population(per_stratum=5)
    result = stratified_sample(
        units, mode="smoke", sample_seed=7, dataset_hash=DATASET_HASH, mode_cfg=SMOKE
    )
    # 6 strata * 1 each = 6, topped up to min_total_units=8.
    assert result.report.total_selected == 8
    assert all(c >= 1 for c in result.report.per_stratum_selected.values())


def test_smoke_deterministic_same_seed():
    units = _population()
    a = stratified_sample(
        units, mode="smoke", sample_seed=7, dataset_hash=DATASET_HASH, mode_cfg=SMOKE
    )
    b = stratified_sample(
        units, mode="smoke", sample_seed=7, dataset_hash=DATASET_HASH, mode_cfg=SMOKE
    )
    assert [u.eval_unit_id for u in a.selected] == [u.eval_unit_id for u in b.selected]


def test_seed_changes_selection():
    units = _population(per_stratum=5)
    a = stratified_sample(
        units, mode="smoke", sample_seed=7, dataset_hash=DATASET_HASH, mode_cfg=SMOKE
    )
    b = stratified_sample(
        units, mode="smoke", sample_seed=99, dataset_hash=DATASET_HASH, mode_cfg=SMOKE
    )
    # Different seed should pick a different concrete set (high probability with
    # 5 candidates per stratum); at minimum the call must stay deterministic.
    assert {u.eval_unit_id for u in a.selected} != {u.eval_unit_id for u in b.selected}


def test_empty_strata_reported_undersampled():
    # Only one stratum populated → the other five are undersampled.
    units = [_unit(i, "pair_timeline", "malicious") for i in range(3)]
    result = stratified_sample(
        units, mode="smoke", sample_seed=7, dataset_hash=DATASET_HASH, mode_cfg=SMOKE
    )
    assert "pair_timeline:benign" in result.report.undersampled_strata
    assert "host_egress:mixed" in result.report.undersampled_strata
    # Can't reach min_total of 8 with only 3 units; selects all available.
    assert result.report.total_selected == 3


def test_full_caps_total():
    units = _population(per_stratum=5)
    result = stratified_sample(
        units, mode="full", sample_seed=7, dataset_hash=DATASET_HASH, mode_cfg=FULL
    )
    # 6 strata * 3 = 18, capped to full_unit_cap=10.
    assert result.report.total_selected == 10
    assert result.report.capped is True
    # Per-stratum counts re-tallied after the cap.
    assert sum(result.report.per_stratum_selected.values()) == 10


def test_full_no_cap_when_under_limit():
    units = _population(per_stratum=1)
    result = stratified_sample(
        units, mode="full", sample_seed=7, dataset_hash=DATASET_HASH, mode_cfg=FULL
    )
    assert result.report.total_selected == 6
    assert result.report.capped is False


def test_selected_units_are_real_population_members():
    units = _population()
    ids = {u.eval_unit_id for u in units}
    result = stratified_sample(
        units, mode="smoke", sample_seed=7, dataset_hash=DATASET_HASH, mode_cfg=SMOKE
    )
    assert all(u.eval_unit_id in ids for u in result.selected)
    # No duplicates.
    selected_ids = [u.eval_unit_id for u in result.selected]
    assert len(selected_ids) == len(set(selected_ids))

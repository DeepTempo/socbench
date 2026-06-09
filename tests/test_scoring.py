"""Step 6 scoring tests."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from socbench.models import EvalUnit
from socbench.scoring import (
    GoldIndex,
    detect_defect,
    load_gold,
    prf,
    score_unit,
)


@dataclass(frozen=True)
class _Rec:
    src_ip: str
    dst_ip: str
    is_malicious: bool


def _gold(spec: dict[int, tuple[str, str, bool]]) -> GoldIndex:
    # GoldIndex.flows is dict[int, _FlowGold]; the duck-typed _Rec satisfies the
    # attribute access scoring performs without reaching into the private type.
    return GoldIndex(
        flows={fid: _Rec(src, dst, mal) for fid, (src, dst, mal) in spec.items()}  # type: ignore[misc]
    )


def _host_unit(flow_ids: list[int], mal: int, distinct: int = 2) -> EvalUnit:
    return EvalUnit(
        eval_unit_id="he-test",
        unit_type="host_egress",
        src_ip="A",
        dst_ip=None,
        flow_ids=flow_ids,
        flow_count=len(flow_ids),
        malicious_flow_count=mal,
        gold_label="mixed" if 0 < mal < len(flow_ids) else ("malicious" if mal else "benign"),
        ts_start_min=0.0,
        ts_start_max=1.0,
        distinct_destinations=distinct,
    )


# --- prf conventions -------------------------------------------------------


def test_prf_empty_is_perfect():
    s = prf(0, 0, 0)
    assert (s.precision, s.recall, s.f1) == (1.0, 1.0, 1.0)


def test_prf_no_gold_recall_is_one():
    # 2 false positives, nothing to find → precision 0, recall 1.
    s = prf(0, 2, 0)
    assert s.precision == 0.0
    assert s.recall == 1.0
    assert s.f1 == 0.0


def test_prf_no_predictions_precision_is_one():
    s = prf(0, 0, 3)
    assert s.precision == 1.0
    assert s.recall == 0.0


def test_prf_balanced():
    s = prf(1, 1, 1)
    assert s.precision == 0.5
    assert s.recall == 0.5
    assert s.f1 == pytest.approx(0.5)


# --- score_unit across the three lenses ------------------------------------


def test_score_unit_perfect():
    gold = _gold(
        {
            0: ("A", "X", True),
            1: ("A", "X", False),
            2: ("A", "Y", True),
        }
    )
    unit = _host_unit([0, 1, 2], mal=2)
    score = score_unit(unit, [0, 2], gold, verdict="malicious")
    assert score.per_flow.f1 == 1.0
    assert score.per_pair.f1 == 1.0
    assert score.per_host.f1 == 1.0
    assert score.defect is None
    assert score.predicted_in_scope == [0, 2]
    assert score.gold_malicious_in_scope == [0, 2]


def test_score_unit_partial_host_lens_coarser():
    gold = _gold(
        {
            0: ("A", "X", True),
            1: ("A", "X", False),
            2: ("A", "Y", True),
        }
    )
    unit = _host_unit([0, 1, 2], mal=2)
    score = score_unit(unit, [0], gold, verdict="malicious")
    # per-flow: tp=1 fp=0 fn=1
    assert score.per_flow.precision == 1.0
    assert score.per_flow.recall == pytest.approx(0.5)
    # per-pair: only (A,X) found, (A,Y) missed
    assert score.per_pair.recall == pytest.approx(0.5)
    # per-host: single host A correctly flagged → perfect despite missed flow
    assert score.per_host.f1 == 1.0


def test_score_unit_clamps_out_of_scope_predictions():
    gold = _gold({0: ("A", "X", True), 1: ("A", "X", False)})
    unit = _host_unit([0, 1], mal=1)
    # 999 is outside the unit's seeded flow set → ignored entirely.
    score = score_unit(unit, [0, 999], gold, verdict="malicious")
    assert score.predicted_in_scope == [0]
    assert score.per_flow.f1 == 1.0


def test_score_unit_benign_clean():
    gold = _gold({0: ("A", "X", False), 1: ("A", "X", False)})
    unit = _host_unit([0, 1], mal=0)
    score = score_unit(unit, [], gold, verdict="benign")
    assert score.per_flow.f1 == 1.0
    assert score.per_pair.f1 == 1.0
    assert score.per_host.f1 == 1.0


def test_score_unit_benign_false_positive():
    gold = _gold({0: ("A", "X", False), 1: ("A", "X", False)})
    unit = _host_unit([0, 1], mal=0)
    score = score_unit(unit, [0], gold, verdict="malicious")
    # false positive on a clean unit → precision 0, recall 1 (nothing to miss)
    assert score.per_flow.precision == 0.0
    assert score.per_flow.recall == 1.0


# --- malicious_destinations shorthand (host_egress fan-out) -----------------


def test_score_unit_destination_shorthand_expands_to_flows():
    # Dest Y is fully malicious; naming Y (no explicit indices) should score
    # the same as enumerating flows 2 and 3.
    gold = _gold(
        {
            0: ("A", "X", False),
            1: ("A", "X", False),
            2: ("A", "Y", True),
            3: ("A", "Y", True),
        }
    )
    unit = _host_unit([0, 1, 2, 3], mal=2)
    score = score_unit(
        unit, [], gold, verdict="malicious", predicted_destinations=["Y"]
    )
    assert score.predicted_in_scope == [2, 3]
    assert score.per_flow.f1 == 1.0
    # malicious verdict backed only by destinations is NOT a defect.
    assert score.defect is None


def test_score_unit_destination_unions_with_explicit_indices():
    gold = _gold(
        {
            0: ("A", "X", True),
            1: ("A", "Y", True),
            2: ("A", "Y", True),
        }
    )
    unit = _host_unit([0, 1, 2], mal=3)
    score = score_unit(
        unit, [0], gold, verdict="malicious", predicted_destinations=["Y"]
    )
    assert score.predicted_in_scope == [0, 1, 2]
    assert score.per_flow.f1 == 1.0


def test_score_unit_destination_with_mixed_flows_costs_precision():
    # Naming X claims all of X malicious, but flow 1 is benign → false positive.
    gold = _gold({0: ("A", "X", True), 1: ("A", "X", False)})
    unit = _host_unit([0, 1], mal=1)
    score = score_unit(
        unit, [], gold, verdict="malicious", predicted_destinations=["X"]
    )
    assert score.predicted_in_scope == [0, 1]
    assert score.per_flow.precision == pytest.approx(0.5)
    assert score.per_flow.recall == 1.0


def test_score_unit_destination_clamped_to_scope():
    gold = _gold({0: ("A", "Y", True), 1: ("A", "Y", True)})
    unit = _host_unit([0], mal=1)  # only flow 0 in scope
    score = score_unit(
        unit, [], gold, verdict="malicious", predicted_destinations=["Y"]
    )
    assert score.predicted_in_scope == [0]


# --- observed-citation clamp (no credit for guessed ids) --------------------


def test_score_unit_drops_unobserved_flow_ids():
    # Flow 2 is genuinely malicious & in scope, but the model never saw it via a
    # tool (observed_flow_ids omits it) → it must NOT count as a prediction.
    gold = _gold({0: ("A", "X", False), 1: ("A", "X", False), 2: ("A", "Y", True)})
    unit = _host_unit([0, 1, 2], mal=1)
    score = score_unit(
        unit, [2], gold, verdict="malicious", observed_flow_ids={0, 1}
    )
    # Guess dropped → no true positive; recall falls (gold flow 2 missed).
    assert score.predicted_in_scope == []
    assert score.per_flow.recall == 0.0


def test_score_unit_keeps_observed_flow_ids():
    gold = _gold({0: ("A", "X", False), 1: ("A", "X", False), 2: ("A", "Y", True)})
    unit = _host_unit([0, 1, 2], mal=1)
    score = score_unit(
        unit, [2], gold, verdict="malicious", observed_flow_ids={0, 1, 2}
    )
    assert score.predicted_in_scope == [2]
    assert score.per_flow.f1 == 1.0


def test_score_unit_drops_unobserved_destinations():
    gold = _gold({0: ("A", "Y", True), 1: ("A", "Y", True)})
    unit = _host_unit([0, 1], mal=2)
    # Names Y but never saw Y in any tool response → expansion suppressed.
    score = score_unit(
        unit,
        [],
        gold,
        verdict="malicious",
        predicted_destinations=["Y"],
        observed_destinations=set(),
    )
    assert score.predicted_in_scope == []


# --- defect detection ------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "predicted", "expected"),
    [
        ("benign", [1], "verdict_indices_mismatch"),
        ("malicious", [], "verdict_indices_mismatch"),
        ("malicious", [1], None),
        ("benign", [], None),
        (None, [1], None),
    ],
)
def test_detect_defect(verdict, predicted, expected):
    assert detect_defect(verdict, predicted) == expected


# --- load_gold integration -------------------------------------------------


def test_load_gold_reads_index(built_index):
    gold = load_gold(built_index.index_dir)
    assert len(gold.flows) > 0
    # The synthetic generator emits malicious scanner/brute-force flows.
    assert any(g.is_malicious for g in gold.flows.values())
    # Every record has a src/dst pair populated.
    sample = next(iter(gold.flows.values()))
    assert sample.src_ip and sample.dst_ip


def test_load_gold_missing_index_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_gold(tmp_path)

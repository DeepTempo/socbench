"""pydantic contract round-trips."""
from __future__ import annotations

import pytest

from socbench.models import (
    EvalUnit,
    RenderingResult,
    SubmitAssessment,
)


def test_submit_assessment_strict() -> None:
    a = SubmitAssessment(
        verdict="malicious",
        confidence=0.91,
        malicious_flow_indices=[0, 2, 3],
        rationale="three flows hit a port-scan pattern",
    )
    assert a.verdict == "malicious"


def test_submit_assessment_rejects_duplicate_indices() -> None:
    with pytest.raises(ValueError):
        SubmitAssessment(
            verdict="malicious",
            confidence=0.5,
            malicious_flow_indices=[1, 1, 2],
            rationale="x",
        )


def test_submit_assessment_accepts_destinations() -> None:
    a = SubmitAssessment(
        verdict="malicious",
        confidence=0.8,
        malicious_destinations=["10.0.0.5", "10.0.0.6"],
        rationale="fan-out to two malicious destinations",
    )
    assert a.malicious_flow_indices == []
    assert a.malicious_destinations == ["10.0.0.5", "10.0.0.6"]


def test_submit_assessment_rejects_duplicate_destinations() -> None:
    with pytest.raises(ValueError):
        SubmitAssessment(
            verdict="malicious",
            confidence=0.5,
            malicious_destinations=["10.0.0.5", "10.0.0.5"],
            rationale="x",
        )


def test_eval_unit_pair_timeline_requires_dst_ip() -> None:
    with pytest.raises(ValueError):
        EvalUnit(
            eval_unit_id="pt-x",
            unit_type="pair_timeline",
            src_ip="1.2.3.4",
            dst_ip=None,
            flow_ids=[0, 1, 2],
            flow_count=3,
            malicious_flow_count=0,
            gold_label="benign",
            ts_start_min=0.0,
            ts_start_max=1.0,
            distinct_destinations=1,
        )


def test_eval_unit_host_egress_rejects_dst_ip() -> None:
    with pytest.raises(ValueError):
        EvalUnit(
            eval_unit_id="he-x",
            unit_type="host_egress",
            src_ip="1.2.3.4",
            dst_ip="9.9.9.9",  # must be None for host_egress
            flow_ids=[0],
            flow_count=1,
            malicious_flow_count=1,
            gold_label="malicious",
            ts_start_min=0.0,
            ts_start_max=0.0,
            distinct_destinations=1,
        )


def test_rendering_result_cap_reason_consistency() -> None:
    ok = RenderingResult(
        rendering_id="r-1",
        eval_unit_id="pt-1",
        provider="mock",
        persona="soc_analyst",
        turns_used=4,
        tool_calls_used=6,
        wall_time_ms=1234,
        cost_usd=0.01,
        cap_hit=True,
        cap_hit_reason="turns",
        final_valid=True,
        forced_final_answer=True,
    )
    assert ok.cap_hit_reason == "turns"

    with pytest.raises(ValueError):
        RenderingResult(
            rendering_id="r-1",
            eval_unit_id="pt-1",
            provider="mock",
            persona="soc_analyst",
            turns_used=4,
            tool_calls_used=6,
            wall_time_ms=1234,
            cost_usd=0.01,
            cap_hit=True,
            cap_hit_reason=None,
            final_valid=True,
            forced_final_answer=True,
        )

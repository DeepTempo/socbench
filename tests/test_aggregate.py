"""Step 6 ablation-aggregator tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from socbench.aggregate import aggregate_ablations, discover_runs

DATASET_HASH = "deadbeef"
SEED = 7


def _write_run(
    runs_root: Path,
    run_id: str,
    *,
    ablation: str,
    per_flow_f1: float,
    cost_usd: float,
    dataset_hash: str = DATASET_HASH,
    seed: int = SEED,
) -> None:
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "dataset_hash": dataset_hash,
                "sample_seed": seed,
                "ablation": ablation,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "scoring": {
                    "mock/soc_analyst": {
                        "units": 4,
                        "per_flow_f1_macro": per_flow_f1,
                        "per_pair_f1_macro": per_flow_f1,
                        "per_host_f1_macro": per_flow_f1,
                        "first_pass_valid_rate": 1.0,
                        "defect_count": 0,
                    }
                },
                "per_provider_persona": {
                    "mock/soc_analyst": {"cost_usd": cost_usd}
                },
            }
        ),
        encoding="utf-8",
    )


def test_discover_runs_filters_and_picks_latest(tmp_path):
    runs_root = tmp_path / "runs"
    _write_run(runs_root, "20260101T000000Z_main_a", ablation="main", per_flow_f1=0.5, cost_usd=1.0)
    _write_run(runs_root, "20260102T000000Z_main_b", ablation="main", per_flow_f1=0.9, cost_usd=1.0)
    # Different dataset_hash → excluded.
    _write_run(
        runs_root,
        "20260103T000000Z_main_c",
        ablation="main",
        per_flow_f1=0.1,
        cost_usd=1.0,
        dataset_hash="other",
    )
    found = discover_runs(runs_root, dataset_hash=DATASET_HASH, sample_seed=SEED)
    assert set(found) == {"main"}
    # Latest run_id (lexical max) wins.
    assert found["main"].run_id == "20260102T000000Z_main_b"


def test_aggregate_computes_deltas(tmp_path):
    runs_root = tmp_path / "runs"
    ablations_root = tmp_path / "ablations"
    _write_run(runs_root, "20260101T000000Z_main", ablation="main", per_flow_f1=0.9, cost_usd=2.0)
    _write_run(
        runs_root,
        "20260101T000001Z_tools_off",
        ablation="tools_off",
        per_flow_f1=0.6,
        cost_usd=1.0,
    )
    out_path = aggregate_ablations(
        runs_root=runs_root,
        ablations_root=ablations_root,
        dataset_hash=DATASET_HASH,
        sample_seed=SEED,
    )
    summary = json.loads(out_path.read_text(encoding="utf-8"))
    delta = summary["deltas"]["tools_off_to_main"]["mock/soc_analyst"]
    assert delta["per_flow_f1"] == pytest.approx(0.3)
    assert delta["cost_usd"] == pytest.approx(1.0)  # main 2.0 - tools_off 1.0
    assert "playbooks_off" in summary["missing_ablations"]
    assert "single_shot_baseline" in summary["missing_ablations"]


def test_aggregate_writes_pointer_files(tmp_path):
    runs_root = tmp_path / "runs"
    ablations_root = tmp_path / "ablations"
    _write_run(runs_root, "20260101T000000Z_main", ablation="main", per_flow_f1=0.9, cost_usd=2.0)
    aggregate_ablations(
        runs_root=runs_root,
        ablations_root=ablations_root,
        dataset_hash=DATASET_HASH,
        sample_seed=SEED,
    )
    out_dir = ablations_root / DATASET_HASH / str(SEED)
    pointer = out_dir / "main_run_id.txt"
    assert pointer.exists()
    assert pointer.read_text(encoding="utf-8").strip() == "20260101T000000Z_main"


def test_aggregate_requires_main(tmp_path):
    runs_root = tmp_path / "runs"
    ablations_root = tmp_path / "ablations"
    _write_run(
        runs_root,
        "20260101T000000Z_tools_off",
        ablation="tools_off",
        per_flow_f1=0.6,
        cost_usd=1.0,
    )
    with pytest.raises(FileNotFoundError):
        aggregate_ablations(
            runs_root=runs_root,
            ablations_root=ablations_root,
            dataset_hash=DATASET_HASH,
            sample_seed=SEED,
        )

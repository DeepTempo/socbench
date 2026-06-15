"""Step 6: ablation aggregator (Step C).

Reads every ``runs/<run_id>/`` sharing a ``(dataset_hash, sample_seed)``,
groups them by their ``ablation`` tag, and writes::

    ablations/<dataset_hash>/<seed>/
    ├── main_run_id.txt
    ├── tools_off_run_id.txt
    ├── playbooks_off_run_id.txt
    ├── single_shot_baseline_run_id.txt
    └── ablation_summary.json

``ablation_summary.json`` reports, per ``(provider, persona, lens)``, the
delta of each ablation against ``main`` (``main - ablation``; positive means
``main`` scores higher). Cheap and safe to re-run; the latest run for each
ablation tag wins (``run_id`` is timestamp-prefixed, so lexical max == most
recent).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from socbench.logging_config import get_logger

log = get_logger(__name__)

_ABLATION_TAGS = ("main", "tools_off", "playbooks_off", "single_shot_baseline")
_POINTER_FILENAMES = {
    "main": "main_run_id.txt",
    "tools_off": "tools_off_run_id.txt",
    "playbooks_off": "playbooks_off_run_id.txt",
    "single_shot_baseline": "single_shot_baseline_run_id.txt",
}
_LENS_KEYS = ("per_flow_f1_macro", "per_pair_f1_macro", "per_host_f1_macro")


@dataclass(frozen=True)
class _RunRef:
    run_id: str
    run_dir: Path
    metadata: dict[str, Any]
    summary: dict[str, Any]


def _load_run(run_dir: Path) -> _RunRef | None:
    """Load a run's metadata + summary, or None if either is missing/invalid."""
    meta_path = run_dir / "run_metadata.json"
    summary_path = run_dir / "summary.json"
    if not (meta_path.exists() and summary_path.exists()):
        return None
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("skipping run with unreadable artifacts", extra={"run_dir": str(run_dir)})
        return None
    return _RunRef(run_id=run_dir.name, run_dir=run_dir, metadata=metadata, summary=summary)


def discover_runs(
    runs_root: Path, *, dataset_hash: str, sample_seed: int
) -> dict[str, _RunRef]:
    """Return ``{ablation_tag: latest_run}`` for the given reproducibility pair."""
    if not runs_root.exists():
        return {}
    by_tag: dict[str, _RunRef] = {}
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        ref = _load_run(run_dir)
        if ref is None:
            continue
        if ref.metadata.get("dataset_hash") != dataset_hash:
            continue
        if int(ref.metadata.get("sample_seed", -1)) != sample_seed:
            continue
        tag = ref.metadata.get("ablation")
        if tag not in _ABLATION_TAGS:
            continue
        # run_id is timestamp-prefixed → lexical max is the most recent.
        existing = by_tag.get(tag)
        if existing is None or ref.run_id > existing.run_id:
            by_tag[tag] = ref
    return by_tag


def _scoring_block(ref: _RunRef) -> dict[str, dict[str, float]]:
    """Per-(provider/persona) scoring dict, tolerant of older runs."""
    block = ref.summary.get("scoring", {})
    return block if isinstance(block, dict) else {}


def _cost_block(ref: _RunRef) -> dict[str, dict[str, Any]]:
    block = ref.summary.get("per_provider_persona", {})
    return block if isinstance(block, dict) else {}


def _delta_against_main(main: _RunRef, other: _RunRef) -> dict[str, dict[str, float]]:
    """Per-(provider/persona) ``main - other`` deltas.

    Covers the flow-set lenses, `first_pass_valid_rate`, the verdict-level
    metrics (accuracy / f1 / coverage-adjusted recall): the meaningful signal
    for tool-stripping ablations where the flow lenses are structurally zero),
    and cost. Missing or ``None`` metrics on either side are skipped.
    """
    main_scores = _scoring_block(main)
    other_scores = _scoring_block(other)
    main_costs = _cost_block(main)
    other_costs = _cost_block(other)

    keys = sorted(set(main_scores) & set(other_scores))
    deltas: dict[str, dict[str, float]] = {}
    for key in keys:
        m = main_scores[key]
        o = other_scores[key]
        row: dict[str, float] = {}
        for lens in _LENS_KEYS:
            if lens in m and lens in o:
                row[lens.replace("_macro", "")] = round(float(m[lens]) - float(o[lens]), 6)
        if "first_pass_valid_rate" in m and "first_pass_valid_rate" in o:
            row["first_pass_valid_rate"] = round(
                float(m["first_pass_valid_rate"]) - float(o["first_pass_valid_rate"]), 6
            )
        # Verdict-level deltas. These are the meaningful efficacy signal for
        # tool-stripping ablations (tools_off / single_shot_baseline), where the
        # flow lenses are structurally ~0 because the model can't observe and
        # cite flow_ids without tools. None-valued metrics (positive-free groups)
        # are skipped rather than coerced.
        mv = m.get("verdict") if isinstance(m.get("verdict"), dict) else {}
        ov = o.get("verdict") if isinstance(o.get("verdict"), dict) else {}
        for vk in ("accuracy", "f1", "coverage_adjusted_recall"):
            if isinstance(mv.get(vk), int | float) and isinstance(ov.get(vk), int | float):
                row[f"verdict_{vk}"] = round(float(mv[vk]) - float(ov[vk]), 6)
        # Cost delta (main - other); negative means main is cheaper.
        if key in main_costs and key in other_costs:
            mc = float(main_costs[key].get("cost_usd", 0.0))
            oc = float(other_costs[key].get("cost_usd", 0.0))
            row["cost_usd"] = round(mc - oc, 6)
        deltas[key] = row
    return deltas


def aggregate_ablations(
    *,
    runs_root: Path,
    ablations_root: Path,
    dataset_hash: str,
    sample_seed: int,
) -> Path:
    """Join runs by ablation tag and write ``ablation_summary.json``.

    Returns the path to the written summary. Raises ``FileNotFoundError`` if
    no ``main`` run exists for the pair; deltas are meaningless without it.
    """
    by_tag = discover_runs(runs_root, dataset_hash=dataset_hash, sample_seed=sample_seed)
    out_dir = ablations_root / dataset_hash / str(sample_seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pointer files for every tag we found.
    for tag, ref in by_tag.items():
        (out_dir / _POINTER_FILENAMES[tag]).write_text(ref.run_id + "\n", encoding="utf-8")

    if "main" not in by_tag:
        raise FileNotFoundError(
            f"no `main` run found for dataset_hash={dataset_hash} seed={sample_seed}; "
            f"found tags: {sorted(by_tag)}"
        )

    main = by_tag["main"]
    deltas: dict[str, dict[str, dict[str, float]]] = {}
    missing: list[str] = []
    for tag in ("tools_off", "playbooks_off", "single_shot_baseline"):
        if tag in by_tag:
            deltas[f"{tag}_to_main"] = _delta_against_main(main, by_tag[tag])
        else:
            missing.append(tag)

    summary = {
        "dataset_hash": dataset_hash,
        "sample_seed": sample_seed,
        "runs": {tag: ref.run_id for tag, ref in sorted(by_tag.items())},
        "main_scoring": _scoring_block(main),
        "deltas": deltas,
        "missing_ablations": missing,
    }
    out_path = out_dir / "ablation_summary.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    log.info(
        "wrote ablation_summary.json",
        extra={
            "path": str(out_path),
            "tags_found": sorted(by_tag),
            "missing": missing,
        },
    )
    return out_path


__all__ = ["aggregate_ablations", "discover_runs"]

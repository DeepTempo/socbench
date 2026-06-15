"""Step 6 — scoring.

Three co-primary lenses, all computed against the eval unit's seeded gold
flow set:

- **per-flow**  — predicted-malicious membership vs. gold, flow by flow.
- **per-IP-pair** — a distinct ``(src_ip, dst_ip)`` is "malicious" if any of
  its flows is; precision/recall/F1 over the distinct pairs in the unit.
- **per-host**  — same, keyed by ``src_ip`` (the meaningful lens for
  ``host_egress`` units that fan out across destinations).

Gold lives in the content-addressed index (``flows.parquet`` keeps the
``_is_malicious`` / ``_flow_id`` columns that the tool layer strips). The
scorer reads it directly — it is the only component besides the index
builder allowed to see ground truth.

Scoring boundary: predictions are clamped to the unit's seeded
``flow_ids``. Anything the model cites outside that set is out of scope and
ignored — the eval unit, not the model's wandering, defines what counts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from socbench.models import EvalUnit, Verdict

# ---------------------------------------------------------------------------
# Gold index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FlowGold:
    src_ip: str
    dst_ip: str
    is_malicious: bool


@dataclass(frozen=True)
class GoldIndex:
    """flow_id → (src_ip, dst_ip, is_malicious), loaded once per run.

    Constructed via :func:`load_gold`. Lookups are pure dict access so a
    Runner can score thousands of units without touching disk again.
    """

    flows: dict[int, _FlowGold]

    def malicious_flow_ids(self, flow_ids: list[int]) -> set[int]:
        """Subset of ``flow_ids`` that are gold-malicious."""
        return {fid for fid in flow_ids if self.flows.get(fid, _BENIGN_SENTINEL).is_malicious}

    def flow_ids_for_destinations(
        self, flow_ids: list[int], destinations: set[str]
    ) -> set[int]:
        """In-scope flow_ids whose ``dst_ip`` is in ``destinations``.

        Used to expand the ``malicious_destinations`` shorthand (host_egress
        fan-out) into concrete flows. ``dst_ip`` is routing metadata, not a
        label, so this stays within the scorer's ground-truth boundary.
        """
        if not destinations:
            return set()
        return {
            fid
            for fid in flow_ids
            if self.flows.get(fid, _BENIGN_SENTINEL).dst_ip in destinations
        }

    def pair_of(self, flow_id: int) -> tuple[str, str]:
        g = self.flows[flow_id]
        return (g.src_ip, g.dst_ip)

    def host_of(self, flow_id: int) -> str:
        return self.flows[flow_id].src_ip


_BENIGN_SENTINEL = _FlowGold(src_ip="", dst_ip="", is_malicious=False)


def load_gold(index_dir: str | Path) -> GoldIndex:
    """Read ``flows.parquet`` and build a :class:`GoldIndex`.

    Uses DuckDB so the read stays fast and lazy even on large indexes; only
    the four columns scoring needs are pulled into memory.
    """
    flows_path = Path(index_dir) / "flows.parquet"
    if not flows_path.exists():
        raise FileNotFoundError(f"flows.parquet not found at {flows_path}")
    con = duckdb.connect(database=":memory:")
    try:
        rows = con.execute(
            "SELECT _flow_id, src_ip, dst_ip, _is_malicious "
            "FROM read_parquet(?)",
            [str(flows_path)],
        ).fetchall()
    finally:
        con.close()
    flows = {
        int(fid): _FlowGold(src_ip=str(src), dst_ip=str(dst), is_malicious=bool(mal))
        for (fid, src, dst, mal) in rows
    }
    return GoldIndex(flows=flows)


# ---------------------------------------------------------------------------
# Metric primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LensScore:
    precision: float
    recall: float
    f1: float

    def as_dict(self, prefix: str) -> dict[str, float]:
        return {
            f"{prefix}_precision": self.precision,
            f"{prefix}_recall": self.recall,
            f"{prefix}_f1": self.f1,
        }


def prf(tp: int, fp: int, fn: int) -> LensScore:
    """Precision / recall / F1 from confusion counts.

    Conventions for the degenerate cases (chosen so a clean benign unit
    scores 1.0 rather than 0.0):

    - No positives predicted and none in gold ``(0,0,0)`` → perfect ``1.0``.
    - Predictions but no gold positives → recall is conventionally ``1.0``
      (nothing to miss); precision falls with false positives.
    - Gold positives but no predictions → precision conventionally ``1.0``
      (nothing wrongly flagged); recall falls to ``0.0``.
    """
    if tp == 0 and fp == 0 and fn == 0:
        return LensScore(1.0, 1.0, 1.0)
    precision = 1.0 if (tp + fp) == 0 else tp / (tp + fp)
    recall = 1.0 if (tp + fn) == 0 else tp / (tp + fn)
    f1 = 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)
    return LensScore(precision, recall, f1)


def _lens_from_sets(predicted: set, gold: set) -> LensScore:
    tp = len(predicted & gold)
    fp = len(predicted - gold)
    fn = len(gold - predicted)
    return prf(tp, fp, fn)


# ---------------------------------------------------------------------------
# Unit scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitScore:
    per_flow: LensScore
    per_pair: LensScore
    per_host: LensScore
    predicted_in_scope: list[int]
    gold_malicious_in_scope: list[int]
    defect: str | None

    def metric_fields(self) -> dict[str, float]:
        """Flat dict matching the ``EvalUnitSummary`` metric field names."""
        out: dict[str, float] = {}
        out.update(self.per_flow.as_dict("per_flow"))
        out.update(self.per_pair.as_dict("per_pair"))
        out.update(self.per_host.as_dict("per_host"))
        return out


def detect_defect(verdict: Verdict | None, predicted_in_scope: list[int]) -> str | None:
    """Defect detection.

    ``predicted_in_scope`` is the *effective* malicious set — explicit
    ``malicious_flow_indices`` unioned with the expansion of
    ``malicious_destinations`` — so a malicious verdict backed only by
    destinations is not a defect.

    - ``verdict=benign`` with non-empty set → ``verdict_indices_mismatch``
      (indices are still used for per-flow metrics; only flagged).
    - ``verdict=malicious`` with empty set → ``verdict_indices_mismatch``
      (per-flow recall counts as 0 naturally).
    """
    if verdict == "benign" and predicted_in_scope:
        return "verdict_indices_mismatch"
    if verdict == "malicious" and not predicted_in_scope:
        return "verdict_indices_mismatch"
    return None


def score_unit(
    unit: EvalUnit,
    predicted_flow_ids: list[int],
    gold: GoldIndex,
    *,
    verdict: Verdict | None = None,
    predicted_destinations: list[str] | None = None,
    observed_flow_ids: set[int] | None = None,
    observed_destinations: set[str] | None = None,
) -> UnitScore:
    """Score one eval unit across all three lenses.

    The effective predicted-malicious set is the union of
    ``predicted_flow_ids`` and the flows reached by expanding
    ``predicted_destinations`` (the host_egress fan-out shorthand). It is
    clamped to the unit's seeded ``flow_ids`` before scoring.

    When ``observed_flow_ids`` / ``observed_destinations`` are supplied (the
    flow_ids and dst_ips the model actually saw in this rendering's tool
    responses), predictions are additionally clamped to them — enforcing the
    kickoff rule that every cited flow must appear in a tool response, so a
    model can't earn precision by guessing in-scope ids it never investigated.
    Gold stays the full in-scope malicious set, so recall still penalises
    malicious flows the model failed to surface. ``None`` disables the clamp.
    """
    scope = set(unit.flow_ids)
    predicted = {int(f) for f in predicted_flow_ids} & scope
    if observed_flow_ids is not None:
        predicted &= observed_flow_ids
    dests = set(predicted_destinations or [])
    if observed_destinations is not None:
        dests &= observed_destinations
    predicted |= gold.flow_ids_for_destinations(unit.flow_ids, dests)
    gold_mal = gold.malicious_flow_ids(unit.flow_ids)

    # per-flow
    per_flow = _lens_from_sets(predicted, gold_mal)

    # per-pair: a pair is positive (gold or predicted) if any of its flows is.
    gold_pairs = {gold.pair_of(fid) for fid in gold_mal}
    pred_pairs = {gold.pair_of(fid) for fid in predicted}
    per_pair = _lens_from_sets(pred_pairs, gold_pairs)

    # per-host: same keyed by src_ip.
    gold_hosts = {gold.host_of(fid) for fid in gold_mal}
    pred_hosts = {gold.host_of(fid) for fid in predicted}
    per_host = _lens_from_sets(pred_hosts, gold_hosts)

    return UnitScore(
        per_flow=per_flow,
        per_pair=per_pair,
        per_host=per_host,
        predicted_in_scope=sorted(predicted),
        gold_malicious_in_scope=sorted(gold_mal),
        defect=detect_defect(verdict, sorted(predicted)),
    )


__all__ = [
    "GoldIndex",
    "LensScore",
    "UnitScore",
    "detect_defect",
    "load_gold",
    "prf",
    "score_unit",
]

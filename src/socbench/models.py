"""Pydantic v2 contracts for socbench.

Every interface across the harness (index → tools → agent loop → scoring →
artifacts) flows through these models. Adding fields is forward-compatible;
removing or renaming them is a breaking change and bumps a manifest hash.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Type aliases (kept here so callers can `from socbench.models import ...`)
# ---------------------------------------------------------------------------

Verdict = Literal["benign", "malicious"]
EvalUnitType = Literal["pair_timeline", "host_egress"]
GoldLabel = Literal["benign", "malicious", "mixed"]
CapHitReason = Literal["turns", "tool_calls", "wall_clock", "cost", "context_budget"]
RunMode = Literal["smoke", "full"]
AblationTag = Literal["main", "tools_off", "playbooks_off", "single_shot_baseline"]


# ---------------------------------------------------------------------------
# Flow / Pair / Host  ─ row-level views of the on-disk index artifacts.
# ---------------------------------------------------------------------------


class Flow(BaseModel):
    """One row of ``flows.parquet``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flow_id: Annotated[int, Field(ge=0)]
    src_ip: str
    dst_ip: str
    ts_start: float
    protocol: str
    src_port: Annotated[int, Field(ge=0)]
    dst_port: Annotated[int, Field(ge=0)]
    bytes_in: Annotated[float, Field(ge=0)]
    bytes_out: Annotated[float, Field(ge=0)]
    pkts_in: Annotated[float, Field(ge=0)]
    pkts_out: Annotated[float, Field(ge=0)]
    tcp_flags: str = ""
    flow_duration_ms: Annotated[float, Field(ge=0)]
    sampling_rate: Annotated[int, Field(ge=1)] = 1
    is_malicious: bool = False
    attack_label: str = ""


class Pair(BaseModel):
    """One row of ``pairs.jsonl`` — per ``(src_ip, dst_ip)`` aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str
    src_ip: str
    dst_ip: str
    flow_count: Annotated[int, Field(ge=0)]
    malicious_flow_count: Annotated[int, Field(ge=0)]
    ts_start_min: float
    ts_start_max: float
    bytes_total: Annotated[float, Field(ge=0)]
    pkts_total: Annotated[float, Field(ge=0)]
    distinct_dst_ports: Annotated[int, Field(ge=0)]
    distinct_src_ports: Annotated[int, Field(ge=0)]


class Host(BaseModel):
    """One row of ``hosts.jsonl`` — per ``src_ip`` aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    flow_count: Annotated[int, Field(ge=0)]
    malicious_flow_count: Annotated[int, Field(ge=0)]
    distinct_destinations: Annotated[int, Field(ge=0)]
    distinct_malicious_destinations: Annotated[int, Field(ge=0)]
    ts_start_min: float
    ts_start_max: float
    bytes_out_total: Annotated[float, Field(ge=0)]
    bytes_in_total: Annotated[float, Field(ge=0)]


# ---------------------------------------------------------------------------
# EvalUnit  ─ provider-agnostic, scoring boundary.
# ---------------------------------------------------------------------------


class EvalUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_unit_id: str
    unit_type: EvalUnitType
    src_ip: str
    dst_ip: str | None = None
    flow_ids: Annotated[list[int], Field(min_length=1)]
    flow_count: Annotated[int, Field(ge=1)]
    malicious_flow_count: Annotated[int, Field(ge=0)]
    gold_label: GoldLabel
    ts_start_min: float
    ts_start_max: float
    distinct_destinations: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _consistency(self) -> EvalUnit:
        if self.flow_count != len(self.flow_ids):
            raise ValueError(
                f"flow_count={self.flow_count} but len(flow_ids)={len(self.flow_ids)}"
            )
        if self.malicious_flow_count > self.flow_count:
            raise ValueError("malicious_flow_count cannot exceed flow_count")
        if self.unit_type == "pair_timeline":
            if self.dst_ip is None:
                raise ValueError("pair_timeline units require dst_ip")
            if self.distinct_destinations != 1:
                raise ValueError("pair_timeline units must have distinct_destinations == 1")
        if self.unit_type == "host_egress" and self.dst_ip is not None:
            raise ValueError("host_egress units must not set dst_ip (multi-destination)")
        if self.ts_start_max < self.ts_start_min:
            raise ValueError("ts_start_max < ts_start_min")
        return self


# ---------------------------------------------------------------------------
# Rendering / RenderingResult.
# ---------------------------------------------------------------------------


class Rendering(BaseModel):
    """Per-provider time-contiguous slice of an EvalUnit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rendering_id: str
    eval_unit_id: str
    provider: str
    part_index: Annotated[int, Field(ge=0)] = 0
    part_count: Annotated[int, Field(ge=1)] = 1
    flow_id_list: Annotated[list[int], Field(min_length=1)]
    ts_start_min: float
    ts_start_max: float
    max_flows_per_rendering: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _check_bounds(self) -> Rendering:
        if len(self.flow_id_list) > self.max_flows_per_rendering:
            raise ValueError(
                f"flow_id_list length {len(self.flow_id_list)} exceeds "
                f"max_flows_per_rendering={self.max_flows_per_rendering}"
            )
        if self.part_index >= self.part_count:
            raise ValueError("part_index must be < part_count")
        return self


class RenderingResult(BaseModel):
    """Persisted as one row of ``renderings.jsonl``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rendering_id: str
    eval_unit_id: str
    provider: str
    persona: str
    turns_used: Annotated[int, Field(ge=0)]
    tool_calls_used: Annotated[int, Field(ge=0)]
    wall_time_ms: Annotated[int, Field(ge=0)]
    cost_usd: Annotated[float, Field(ge=0)]
    cap_hit: bool
    cap_hit_reason: CapHitReason | None = None
    final_valid: bool
    forced_final_answer: bool
    adapter_fatal: bool = False

    @model_validator(mode="after")
    def _cap_reason_iff_cap_hit(self) -> RenderingResult:
        if self.cap_hit and self.cap_hit_reason is None:
            raise ValueError("cap_hit=True requires cap_hit_reason")
        if not self.cap_hit and self.cap_hit_reason is not None:
            raise ValueError("cap_hit_reason set but cap_hit=False")
        return self


# ---------------------------------------------------------------------------
# SubmitAssessment  ─ the strict final-answer contract.
# ---------------------------------------------------------------------------


class SubmitAssessment(BaseModel):
    """The strict JSON contract every model returns via ``submit_assessment``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Verdict
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    # Despite the name, these are global ``flow_id`` values (the ids the tools
    # return), not positional offsets into the rendering. Scoring clamps them
    # to the eval unit's seeded flow set.
    malicious_flow_indices: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list)
    # Destination-level shorthand for fan-out (``host_egress``) units: instead
    # of enumerating every malicious flow_id, name the destination IP(s) judged
    # malicious. Scoring expands each to its in-scope flows (unioned with
    # ``malicious_flow_indices``). Empty for single-pair units.
    malicious_destinations: list[str] = Field(default_factory=list)
    rationale: Annotated[str, Field(min_length=1, max_length=8000)]

    @model_validator(mode="after")
    def _no_duplicates(self) -> SubmitAssessment:
        if len(set(self.malicious_flow_indices)) != len(self.malicious_flow_indices):
            raise ValueError("malicious_flow_indices contains duplicates")
        if len(set(self.malicious_destinations)) != len(self.malicious_destinations):
            raise ValueError("malicious_destinations contains duplicates")
        return self


# ---------------------------------------------------------------------------
# Tool call / result  ─ wire-shape for tool_calls.jsonl.
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rendering_id: str
    turn_index: Annotated[int, Field(ge=0)]
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    args_hash: str


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rendering_id: str
    turn_index: Annotated[int, Field(ge=0)]
    tool_name: str
    ok: bool
    payload: dict[str, Any] | None = None
    error: str | None = None
    truncated: bool = False
    wall_time_ms: Annotated[int, Field(ge=0)] = 0


class PredictionRow(BaseModel):
    """One row of ``predictions_raw.jsonl``.

    Captures the per-turn provider call so that cost, latency, and tool-use
    can be reconstructed without re-running. ``tool_name`` is the tool the
    model issued on this turn (or ``"submit_assessment"`` for the final
    turn, or ``None`` for a plain-text turn).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_unit_id: str
    rendering_id: str
    provider: str
    persona: str
    turn_index: Annotated[int, Field(ge=0)]
    tool_name: str | None = None
    tool_call_args_hash: str | None = None
    tool_result_truncated: bool = False
    prompt_tokens: Annotated[int, Field(ge=0)] = 0
    cached_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    reasoning_tokens: Annotated[int, Field(ge=0)] = 0
    wall_time_ms: Annotated[int, Field(ge=0)] = 0
    cost_usd: Annotated[float, Field(ge=0)] = 0.0


# ---------------------------------------------------------------------------
# Run-level: metadata + per-eval-unit summary + artifact paths.
# ---------------------------------------------------------------------------


class RunMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    # Discriminates this multi-turn agent benchmark from a single-shot baseline
    # run when both write into the shared runs/ directory.
    spec_id: str
    mode: RunMode
    ablation: AblationTag
    dataset_hash: str
    sample_seed: int
    prompts_manifest_sha: str
    playbooks_manifest_sha: str
    tools_manifest_sha: str
    # Exact model id each provider resolved to at run time (e.g.
    # {"anthropic": "claude-opus-4-7"}). Persisted for provenance so results are
    # reproducible without back-calculating the model from token costs.
    provider_models: dict[str, str] = Field(default_factory=dict)
    pricing_snapshot_date: str
    cost_usd_cap_per_rendering: Annotated[float, Field(gt=0)]
    cost_budget_usd: Annotated[float, Field(gt=0)]
    image_tag: str
    git_sha: str | None = None
    host: str
    started_at_utc: str
    finished_at_utc: str | None = None


class EvalUnitSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_unit_id: str
    unit_type: str
    gold_label: str
    provider: str
    persona: str
    cost_usd: Annotated[float, Field(ge=0)]
    wall_time_ms: Annotated[int, Field(ge=0)] = 0
    renderings_count: Annotated[int, Field(ge=1)]
    unit_first_pass_valid: bool

    # Final-answer summary. ``verdict`` mirrors the SubmitAssessment the
    # rendering produced (None when no valid submission was made); ``defect``
    # carries a single diagnostic tag such as ``verdict_indices_mismatch``.
    verdict: Verdict | None = None
    defect: str | None = None

    # Remaining fields of the SubmitAssessment contract, preserved verbatim so
    # the model's stated confidence and reasoning are never silently dropped.
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    rationale: str | None = None

    # Effective, post-expansion, in-scope malicious set used for scoring.
    predicted_malicious_flow_ids: list[int] = Field(default_factory=list)
    # Raw, as-submitted contract values (before destination expansion or
    # scope-clamping) so mechanism usage is auditable without parsing prose:
    # which units leaned on the destination shorthand vs. flow enumeration.
    submitted_malicious_flow_indices: list[int] = Field(default_factory=list)
    submitted_malicious_destinations: list[str] = Field(default_factory=list)

    per_flow_precision: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    per_flow_recall: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    per_flow_f1: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0

    per_pair_precision: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    per_pair_recall: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    per_pair_f1: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0

    per_host_precision: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    per_host_recall: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    per_host_f1: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0


class RunArtifactsPaths(BaseModel):
    """Resolved on-disk layout for a single run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    run_metadata: Path
    predictions_raw_jsonl: Path
    predictions_raw_parquet: Path
    predictions_per_flow_parquet: Path
    renderings_jsonl: Path
    eval_units_summary_jsonl: Path
    tool_calls_jsonl: Path
    summary_json: Path
    prompts_used_dir: Path
    index_manifest_link: Path

    @model_validator(mode="after")
    def _all_under_root(self) -> RunArtifactsPaths:
        for p in (
            self.run_metadata,
            self.predictions_raw_jsonl,
            self.predictions_raw_parquet,
            self.predictions_per_flow_parquet,
            self.renderings_jsonl,
            self.eval_units_summary_jsonl,
            self.tool_calls_jsonl,
            self.summary_json,
            self.prompts_used_dir,
            self.index_manifest_link,
        ):
            if self.root not in p.parents and p != self.root:
                raise ValueError(f"{p} is not under run root {self.root}")
        return self


__all__ = [
    "AblationTag",
    "CapHitReason",
    "EvalUnit",
    "EvalUnitSummary",
    "EvalUnitType",
    "Flow",
    "GoldLabel",
    "Host",
    "Pair",
    "PredictionRow",
    "Rendering",
    "RenderingResult",
    "RunArtifactsPaths",
    "RunMetadata",
    "RunMode",
    "SubmitAssessment",
    "ToolCall",
    "ToolResult",
    "Verdict",
]

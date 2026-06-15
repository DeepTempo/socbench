"""Loader and types for the repo-level ``schema.json``.

The shape of ``schema.json`` is part of the public contract: ``dataset_hash``
includes a hash of the canonical schema, so a tweak here invalidates downstream
indexes by design.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from socbench.hashing import hash_obj

CANONICAL_COLUMNS: tuple[str, ...] = (
    "src_ip",
    "dst_ip",
    "ts_start",
    "protocol",
    "src_port",
    "dst_port",
    "bytes_in",
    "bytes_out",
    "pkts_in",
    "pkts_out",
    "tcp_flags",
    "flow_duration_ms",
    "sampling_rate",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Lax(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class TimestampInference(_Strict):
    millisecond_aliases: list[str]
    second_aliases: list[str]
    rule: str


class MixedDatasetRules(_Strict):
    normalized_columns: dict[str, str]
    attack_columns: list[str]
    label_columns: list[str]
    benign_attack_match: str
    malicious_attack_match: str
    numeric_label_rule: str


class LabelInference(_Strict):
    supported_label_groups: list[str]
    mixed_dataset_rules: MixedDatasetRules
    attack_family_strings_used_for_forbidden_token_check: list[str]


class StageAOutputContract(_Strict):
    flows_parquet_columns_appended_by_normalization: list[str]


class CanonicalSchema(_Strict):
    schema_version: str
    title: str
    description: str
    canonical_flow_record: _Lax
    normalization_aliases: dict[str, list[str]]
    timestamp_inference: TimestampInference
    label_inference: LabelInference
    stage_a_output_contract: StageAOutputContract

    @property
    def schema_hash(self) -> str:
        """Stable hash of the schema's contractual surface (excludes title/description)."""
        return hash_obj(
            {
                "schema_version": self.schema_version,
                "canonical_flow_record": self.canonical_flow_record.model_dump(),
                "normalization_aliases": self.normalization_aliases,
                "timestamp_inference": self.timestamp_inference.model_dump(),
                "label_inference": self.label_inference.model_dump(),
                "stage_a_output_contract": self.stage_a_output_contract.model_dump(),
            }
        )

    def aliases_for(self, canonical_col: str) -> list[str]:
        return list(self.normalization_aliases.get(canonical_col, []))

    def resolve_source_column(
        self, canonical_col: str, present_columns: list[str]
    ) -> str | None:
        """First alias for ``canonical_col`` that appears in ``present_columns``.

        Alias order in ``schema.json`` is significant: canonical names sit at
        the top of each list so a dataset that already speaks canonical wins.
        Returns ``None`` when nothing matches; callers decide if that's fatal.
        """
        case_insensitive = {c.lower(): c for c in present_columns}
        for alias in self.aliases_for(canonical_col):
            actual = case_insensitive.get(alias.lower())
            if actual is not None:
                return actual
        return None


def load_schema(path: str | Path) -> CanonicalSchema:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return CanonicalSchema.model_validate(raw)


__all__ = [
    "CANONICAL_COLUMNS",
    "CanonicalSchema",
    "LabelInference",
    "MixedDatasetRules",
    "StageAOutputContract",
    "TimestampInference",
    "load_schema",
]

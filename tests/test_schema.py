"""schema.json round-trip and resolver behaviour."""
from __future__ import annotations

from socbench.schema import CANONICAL_COLUMNS, CanonicalSchema


def test_schema_loads_and_hashes(schema: CanonicalSchema) -> None:
    h1 = schema.schema_hash
    h2 = schema.schema_hash
    assert h1 == h2
    assert len(h1) == 32


def test_aliases_exist_for_every_canonical_column(schema: CanonicalSchema) -> None:
    for col in CANONICAL_COLUMNS:
        assert schema.aliases_for(col), f"no aliases declared for canonical column {col}"
        # The canonical name itself MUST appear at the head of its own alias list,
        # so a dataset that already speaks canonical wins.
        assert schema.aliases_for(col)[0] == col


def test_resolver_prefers_first_alias(schema: CanonicalSchema) -> None:
    present = ["SrcIP", "src_ip", "DstIP"]
    resolved = schema.resolve_source_column("src_ip", present)
    # First alias in the list is "src_ip", so it wins.
    assert resolved == "src_ip"


def test_resolver_falls_back_to_secondary(schema: CanonicalSchema) -> None:
    present = ["IPV4_SRC_ADDR", "IPV4_DST_ADDR", "FLOW_START_MILLISECONDS"]
    assert schema.resolve_source_column("src_ip", present) == "IPV4_SRC_ADDR"
    assert schema.resolve_source_column("ts_start", present) == "FLOW_START_MILLISECONDS"


def test_resolver_returns_none_when_no_match(schema: CanonicalSchema) -> None:
    assert schema.resolve_source_column("src_ip", ["nope"]) is None

"""Tool layer: registry behaviour + per-tool calls against the built index.

The persona × tool matrix is loaded from ``benchmark_config.yaml`` (single
source of truth shared with Stages 4 and 5), so these tests pull expectations
from the actual config rather than from a constant in code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from socbench.config import AgentConfig, load_benchmark_config
from socbench.tools import (
    GROUND_TRUTH_FIELDS,
    GroundTruthLeak,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolSchemaViolation,
    build_default_registry,
    run_smoke,
)
from socbench.tools.base import _assert_no_ground_truth_leak
from socbench.tools.smoke import _default_args, _pick_seed_flow_ids, _pick_seed_pair

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture(scope="session")
def agent_cfg() -> AgentConfig:
    return load_benchmark_config(CONFIG_DIR / "benchmark_config.yaml").agent


@pytest.fixture(scope="session")
def persona_allowlist(agent_cfg: AgentConfig) -> dict[str, set[str]]:
    return agent_cfg.persona_tool_allowlist()


def test_registry_manifest_sha_is_stable(persona_allowlist: dict[str, set[str]]) -> None:
    r1 = build_default_registry(persona_allowlist=persona_allowlist)
    r2 = build_default_registry(persona_allowlist=persona_allowlist)
    assert r1.manifest_sha() == r2.manifest_sha()
    assert len(r1.manifest_sha()) == 32


def test_registry_manifest_sha_changes_with_allowlist(
    persona_allowlist: dict[str, set[str]],
) -> None:
    base = build_default_registry(persona_allowlist=persona_allowlist).manifest_sha()
    tweaked = {**persona_allowlist}
    tweaked["soc_analyst"] = tweaked["soc_analyst"] | {"top_destinations"}
    bumped = build_default_registry(persona_allowlist=tweaked).manifest_sha()
    assert base != bumped, "policy changes must shift tools_manifest_sha"


def test_registry_rejects_unknown_tool_in_allowlist() -> None:
    with pytest.raises(ValueError, match="unknown tools"):
        build_default_registry(
            persona_allowlist={"soc_analyst": {"list_pairs", "made_up_tool"}}
        )


def test_registry_without_allowlist_blocks_persona_queries() -> None:
    reg = build_default_registry()
    with pytest.raises(RuntimeError, match="no persona allowlist"):
        reg.tools_for_persona("soc_analyst")


def test_persona_allowlist_matches_adr_matrix(
    persona_allowlist: dict[str, set[str]],
) -> None:
    assert persona_allowlist["soc_analyst"] == {
        "list_pairs",
        "get_pair_timeline",
        "get_flows",
        "host_rollup",
        "submit_assessment",
    }
    assert persona_allowlist["threat_analyst"] >= {"top_destinations", "pair_stats"}
    for extra in ("port_proto_matrix", "rarity_stats"):
        assert extra in persona_allowlist["adversary_hunter"]
        assert extra in persona_allowlist["detection_engineer"]
    for extra in ("top_destinations", "pair_stats", "port_proto_matrix", "rarity_stats"):
        assert extra not in persona_allowlist["soc_analyst"]


def test_invalid_args_raise_schema_violation(built_index) -> None:  # type: ignore[no-untyped-def]
    reg = build_default_registry()
    ctx = ToolContext(index_dir=built_index.index_dir)
    list_pairs = reg.get("list_pairs")
    with pytest.raises(ToolSchemaViolation):
        list_pairs({"limit": "not-an-int"}, ctx)  # type: ignore[arg-type]


def test_get_flows_returns_known_ids(built_index) -> None:  # type: ignore[no-untyped-def]
    reg = build_default_registry()
    ctx = ToolContext(index_dir=built_index.index_dir)
    payload = reg.get("get_flows")({"flow_ids": [0, 1, 2]}, ctx)
    assert payload["returned"] == 3
    assert payload["missing_flow_ids"] == []
    assert {it["flow_id"] for it in payload["items"]} == {0, 1, 2}


def test_submit_assessment_validates_verdict(built_index) -> None:  # type: ignore[no-untyped-def]
    reg = build_default_registry()
    ctx = ToolContext(index_dir=built_index.index_dir)
    tool = reg.get("submit_assessment")
    with pytest.raises(ToolSchemaViolation):
        tool({"verdict": "uncertain", "confidence": 0.5, "rationale": "x"}, ctx)


@pytest.fixture(scope="session")
def smoke_registry(persona_allowlist: dict[str, set[str]]) -> ToolRegistry:
    return build_default_registry(persona_allowlist=persona_allowlist)


@pytest.mark.parametrize(
    "persona",
    ["soc_analyst", "threat_analyst", "adversary_hunter", "detection_engineer"],
)
def test_smoke_runs_for_every_persona(  # type: ignore[no-untyped-def]
    built_index,
    smoke_registry: ToolRegistry,
    persona_allowlist: dict[str, set[str]],
    persona: str,
) -> None:
    result = run_smoke(
        registry=smoke_registry, persona=persona, index_dir=built_index.index_dir
    )
    assert result["persona"] == persona
    assert set(result["tools"].keys()) == persona_allowlist[persona]
    for tool_name, entry in result["tools"].items():
        assert entry["ok"], f"{persona} / {tool_name} failed: {entry.get('error')}"


# ---------------------------------------------------------------------------
# Ground-truth leak guard (defense in depth: per-tool SQL fixes + boundary net)
# ---------------------------------------------------------------------------


_EXPECTED_GROUND_TRUTH_FIELDS = frozenset(
    {
        "_is_malicious",
        "is_malicious",
        "attack_label",
        "_attack_label",
        "attack_type",
        "_numeric_label",
        "numeric_label",
        "malicious_flow_count",
        "distinct_malicious_destinations",
        "gold_label",
    }
)


def test_ground_truth_field_set_is_locked() -> None:
    """The set is a closed allowlist; expanding it requires a code change.

    A canary on the contract: if someone trims this set they have to update
    this test too, which forces a thoughtful review.
    """
    assert GROUND_TRUTH_FIELDS == _EXPECTED_GROUND_TRUTH_FIELDS


def test_safety_net_passes_clean_payload() -> None:
    _assert_no_ground_truth_leak(
        {"items": [{"flow_id": 1, "bytes_in": 100}], "returned": 1, "limit": 50}
    )


@pytest.mark.parametrize("forbidden", sorted(GROUND_TRUTH_FIELDS))
def test_safety_net_catches_flat_leak(forbidden: str) -> None:
    with pytest.raises(GroundTruthLeak, match=forbidden):
        _assert_no_ground_truth_leak({forbidden: True})


def test_safety_net_catches_nested_leak() -> None:
    payload = {
        "scope": {"src_ip": "10.0.0.1"},
        "items": [
            {"dst_port": 443, "flow_count": 100},
            {"dst_port": 22, "flow_count": 3, "malicious_flow_count": 3},
        ],
    }
    with pytest.raises(GroundTruthLeak, match=r"\$.items\[1\].malicious_flow_count"):
        _assert_no_ground_truth_leak(payload)


def test_tool_boundary_blocks_leaky_tool(built_index) -> None:  # type: ignore[no-untyped-def]
    """A tool that bypasses the SQL fix is still blocked at __call__."""

    class _LeakyTool(Tool):
        name: ClassVar[str] = "_leaky"
        description: ClassVar[str] = "Test-only tool that returns ground truth."
        args_schema: ClassVar[dict[str, Any]] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        }

        def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            return {"items": [{"flow_id": 7, "is_malicious": True}]}

    ctx = ToolContext(index_dir=built_index.index_dir)
    with pytest.raises(GroundTruthLeak, match="is_malicious"):
        _LeakyTool()({}, ctx)


def test_every_tool_sql_does_not_leak(  # type: ignore[no-untyped-def]
    built_index, smoke_registry: ToolRegistry
) -> None:
    """Direct verification of the surgical fixes: invoke each tool's raw
    ``.call()`` (bypassing the boundary safety net) and check the unwrapped
    payload. This proves the per-tool SQL fixes are correct independently of
    whether the boundary assertion fires.
    """
    ctx = ToolContext(index_dir=built_index.index_dir)
    seed_pair = _pick_seed_pair(built_index.index_dir)
    seed_ids = _pick_seed_flow_ids(built_index.index_dir)

    for tool_name in smoke_registry.names():
        tool = smoke_registry.get(tool_name)
        args = _default_args(tool_name, seed_pair, seed_ids)
        tool.validate_args(args)
        raw_payload = tool.call(args, ctx)  # NB: bypasses the boundary net
        _assert_no_ground_truth_leak(raw_payload, path=f"$.{tool_name}")

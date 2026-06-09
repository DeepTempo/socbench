"""Step 4 — prompt loading, compose pipeline, forbidden-token check, manifests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from socbench.prompts import (
    ForbiddenTokenInPrompt,
    PromptParts,
    check_forbidden_tokens,
    compose,
    load_prompts,
    playbooks_manifest_sha,
    prompts_manifest_sha,
)
from socbench.schema import LabelInference, load_schema
from socbench.tools import build_default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "config" / "prompts"
SCHEMA_PATH = REPO_ROOT / "config" / "schema.json"

PERSONA_NAMES = (
    "soc_analyst",
    "threat_analyst",
    "adversary_hunter",
    "detection_engineer",
)


@pytest.fixture(scope="session")
def parts() -> PromptParts:
    return load_prompts(PROMPTS_DIR)


@pytest.fixture(scope="session")
def label_inference() -> LabelInference:
    return load_schema(SCHEMA_PATH).label_inference


@pytest.fixture(scope="session")
def output_contract() -> dict[str, Any]:
    return build_default_registry().get("submit_assessment").args_schema


@pytest.fixture(scope="session")
def all_tool_schemas() -> list[dict[str, Any]]:
    reg = build_default_registry()
    return [reg.get(name).args_schema for name in reg.names()]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_yields_every_persona_and_playbook(parts: PromptParts) -> None:
    assert set(parts.personas) == set(PERSONA_NAMES)
    assert set(parts.playbooks) == set(PERSONA_NAMES)
    assert parts.playbook_common.strip(), "common playbook must not be empty"
    for p in PERSONA_NAMES:
        assert parts.personas[p].strip(), f"persona {p} must not be empty"
        assert parts.playbooks[p].strip(), f"playbook {p} must not be empty"


def test_load_rejects_persona_playbook_mismatch(tmp_path: Path) -> None:
    (tmp_path / "playbook_common.md").write_text("common", encoding="utf-8")
    (tmp_path / "personas").mkdir()
    (tmp_path / "playbooks").mkdir()
    (tmp_path / "personas" / "a.md").write_text("a-persona", encoding="utf-8")
    (tmp_path / "playbooks" / "b.md").write_text("b-playbook", encoding="utf-8")
    with pytest.raises(ValueError, match="filename stems must match"):
        load_prompts(tmp_path)


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------


def test_compose_is_deterministic(
    parts: PromptParts,
    label_inference: LabelInference,
    output_contract: dict[str, Any],
    all_tool_schemas: list[dict[str, Any]],
) -> None:
    kwargs: dict[str, Any] = dict(
        parts=parts,
        persona="soc_analyst",
        ablation="main",
        output_contract_schema=output_contract,
        tool_schemas=all_tool_schemas,
        label_inference=label_inference,
    )
    assert compose(**kwargs) == compose(**kwargs)


@pytest.mark.parametrize("persona", list(PERSONA_NAMES))
def test_compose_for_every_persona(
    parts: PromptParts,
    label_inference: LabelInference,
    output_contract: dict[str, Any],
    all_tool_schemas: list[dict[str, Any]],
    persona: str,
) -> None:
    assembled = compose(
        parts=parts,
        persona=persona,
        ablation="main",
        output_contract_schema=output_contract,
        tool_schemas=all_tool_schemas,
        label_inference=label_inference,
    )
    assert "# Persona\n" in assembled
    assert "# Persona Playbook\n" in assembled
    assert "# Common Playbook\n" in assembled
    assert "# Output Contract\n" in assembled
    assert "# Tools\n" in assembled


def test_compose_playbooks_off_drops_per_persona_playbook(
    parts: PromptParts,
    label_inference: LabelInference,
    output_contract: dict[str, Any],
    all_tool_schemas: list[dict[str, Any]],
) -> None:
    main = compose(
        parts=parts,
        persona="soc_analyst",
        ablation="main",
        output_contract_schema=output_contract,
        tool_schemas=all_tool_schemas,
        label_inference=label_inference,
    )
    off = compose(
        parts=parts,
        persona="soc_analyst",
        ablation="playbooks_off",
        output_contract_schema=output_contract,
        tool_schemas=all_tool_schemas,
        label_inference=label_inference,
    )
    assert "# Persona Playbook\n" in main
    assert "# Persona Playbook\n" not in off
    # persona + common playbook still present under playbooks_off
    assert "# Persona\n" in off
    assert "# Common Playbook\n" in off


def test_compose_unknown_persona_raises(
    parts: PromptParts,
    label_inference: LabelInference,
    output_contract: dict[str, Any],
    all_tool_schemas: list[dict[str, Any]],
) -> None:
    with pytest.raises(KeyError, match="unknown persona"):
        compose(
            parts=parts,
            persona="ghost",
            ablation="main",
            output_contract_schema=output_contract,
            tool_schemas=all_tool_schemas,
            label_inference=label_inference,
        )


# ---------------------------------------------------------------------------
# Forbidden-token check
# ---------------------------------------------------------------------------


def test_check_passes_clean_text(label_inference: LabelInference) -> None:
    check_forbidden_tokens(
        "Investigate the flows in the eval unit and submit a verdict.",
        label_inference=label_inference,
    )


@pytest.mark.parametrize(
    "forbidden_text,expected_pattern",
    [
        ("If the Attack column equals 'yes', flag it.", "label_column"),
        ("Filter where Label == 1.", "label_column"),
        ("If the is_malicious field is true, return malicious.", "label_column"),
        ("Look at malicious_flow_count for the pair.", "label_column"),
        ("Connect to 10.0.0.1 to verify.", "ipv4_literal"),
        ("Check the CIDR 192.168.0.0/16.", "ipv4_literal"),
        ("Watch for DDoS volumetrics in the host_rollup.", "attack_family"),
        ("Look for Bot-style beaconing.", "attack_family"),
        ("Inspect Mirai-family scanners.", "attack_family"),
        ("Match hash deadbeefdeadbeefdeadbeefdeadbeef.", "hex_hash"),
    ],
)
def test_check_catches_forbidden_patterns(
    label_inference: LabelInference, forbidden_text: str, expected_pattern: str
) -> None:
    with pytest.raises(ForbiddenTokenInPrompt, match=expected_pattern):
        check_forbidden_tokens(forbidden_text, label_inference=label_inference)


def test_shipped_content_passes_check(
    parts: PromptParts, label_inference: LabelInference
) -> None:
    """Every fragment we ship under config/prompts/ must be clean.

    This is the contract that lets us check the prompts into the repo with
    confidence. If a future content edit introduces a forbidden token, this
    test fails before the run does.
    """
    check_forbidden_tokens(
        parts.playbook_common, label_inference=label_inference, where="playbook_common"
    )
    for p, text in parts.personas.items():
        check_forbidden_tokens(
            text, label_inference=label_inference, where=f"persona/{p}"
        )
    for p, text in parts.playbooks.items():
        check_forbidden_tokens(
            text, label_inference=label_inference, where=f"playbook/{p}"
        )


# ---------------------------------------------------------------------------
# Manifest hashes
# ---------------------------------------------------------------------------


def test_prompts_manifest_sha_is_stable(parts: PromptParts) -> None:
    h1 = prompts_manifest_sha(parts)
    h2 = prompts_manifest_sha(parts)
    assert h1 == h2
    assert len(h1) == 32


def test_playbooks_manifest_sha_rotates_with_ablation(parts: PromptParts) -> None:
    main = playbooks_manifest_sha(parts, ablation="main")
    tools_off = playbooks_manifest_sha(parts, ablation="tools_off")
    playbooks_off = playbooks_manifest_sha(parts, ablation="playbooks_off")
    assert main == tools_off, "tools_off does not change playbooks content"
    assert main != playbooks_off, "playbooks_off must rotate playbooks hash"
    assert len(playbooks_off) == 32


def test_prompts_manifest_sha_changes_when_persona_block_changes(
    parts: PromptParts,
) -> None:
    base = prompts_manifest_sha(parts)
    mutated = PromptParts(
        playbook_common=parts.playbook_common,
        personas={**parts.personas, "soc_analyst": parts.personas["soc_analyst"] + "x"},
        playbooks=parts.playbooks,
    )
    assert base != prompts_manifest_sha(mutated)


def test_playbooks_manifest_sha_changes_when_playbook_changes(
    parts: PromptParts,
) -> None:
    base = playbooks_manifest_sha(parts, ablation="main")
    mutated = PromptParts(
        playbook_common=parts.playbook_common,
        personas=parts.personas,
        playbooks={
            **parts.playbooks,
            "soc_analyst": parts.playbooks["soc_analyst"] + "x",
        },
    )
    assert base != playbooks_manifest_sha(mutated, ablation="main")

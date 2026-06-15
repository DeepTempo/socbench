"""Step 4 — prompt assembly + forbidden-token check + manifest hashing.

Public entrypoints:

- :func:`load_prompts` — reads the on-disk tree under ``config/prompts/`` and
  returns a :class:`PromptParts` snapshot of the content (single source of
  truth for both compose and the manifest hashes).
- :func:`compose` — assembles the system prompt for one (persona, ablation)
  in the required compose order and runs the forbidden-token check on
  the assembled string before returning.
- :func:`prompts_manifest_sha` / :func:`playbooks_manifest_sha` — content
  hashes recorded in run artifacts so two runs can be checked for
  prompt-side comparability without diffing files.

The forbidden-token check enforces the "process and generic patterns only"
rule for playbooks. The forbidden set is built from three sources, all
centralised so nothing is hardcoded twice:

1. ``socbench.tools.base.GROUND_TRUTH_FIELDS`` — label-derived column names
   that already gate tool responses (the tool-layer leak guard).
2. ``schema.label_inference.{attack_columns, label_columns,
   attack_family_strings_used_for_forbidden_token_check}`` — declarative
   knobs in ``config/schema.json``.
3. Structural regexes for IPv4/IPv6 literals and MD5/SHA1/SHA256 hex hashes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from socbench.hashing import hash_obj
from socbench.schema import LabelInference
from socbench.tools.base import GROUND_TRUTH_FIELDS

Ablation = Literal["main", "tools_off", "playbooks_off"]


# The scaffold is part of ``prompts_manifest_sha``. Edit it the same way you
# would edit any prompt text — the hash will rotate, and downstream comparisons
# will correctly treat pre/post-edit runs as different prefixes.
SYSTEM_SCAFFOLD = (
    "You are a security agent investigating network traffic captured from a "
    "corporate network. You have read-only access to a pre-built corpus index "
    "via a small set of tools. Use the tools to gather evidence, then commit "
    "your final verdict by calling `submit_assessment`. The agent loop ends "
    "the moment that tool returns."
)


# ---------------------------------------------------------------------------
# Forbidden-token check
# ---------------------------------------------------------------------------


class ForbiddenTokenInPrompt(RuntimeError):
    """Raised when assembled prompt content matches a forbidden pattern.

    Fix by editing the offending fragment — never by relaxing this check.
    """


def _build_forbidden_patterns(
    label_inference: LabelInference,
) -> list[tuple[str, re.Pattern[str]]]:
    """Compose the forbidden-pattern list in a deterministic order.

    Column-name patterns are case-SENSITIVE (literal field names); attack-family
    strings are case-INsensitive (catches prose mentions like "ddos" alongside
    "DDoS"); structural literals are pattern-based.
    """
    rules = label_inference.mixed_dataset_rules
    column_names = sorted(
        set(GROUND_TRUTH_FIELDS) | set(rules.attack_columns) | set(rules.label_columns)
    )
    family_strings = sorted(
        set(label_inference.attack_family_strings_used_for_forbidden_token_check)
    )

    patterns: list[tuple[str, re.Pattern[str]]] = [
        ("ipv4_literal", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")),
        (
            "ipv6_literal",
            re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"),
        ),
        ("hex_hash", re.compile(r"\b(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\b")),
    ]
    if column_names:
        joined = "|".join(re.escape(w) for w in column_names)
        patterns.append(("label_column", re.compile(rf"\b(?:{joined})\b")))
    if family_strings:
        joined = "|".join(re.escape(w) for w in family_strings)
        patterns.append(
            ("attack_family", re.compile(rf"\b(?:{joined})\b", re.IGNORECASE))
        )
    return patterns


def check_forbidden_tokens(
    text: str, *, label_inference: LabelInference, where: str = "<unknown>"
) -> None:
    """Scan ``text`` and raise on the first forbidden match.

    First match wins — fix it, re-run; subsequent matches surface one at a
    time. Optimises for clear error messages over batch reporting.
    """
    for name, pattern in _build_forbidden_patterns(label_inference):
        match = pattern.search(text)
        if match:
            raise ForbiddenTokenInPrompt(
                f"prompt fragment {where!r} matches forbidden pattern "
                f"{name!r} at offset {match.start()}: {match.group(0)!r}"
            )


# ---------------------------------------------------------------------------
# Content loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptParts:
    """In-memory snapshot of the prompt content tree on disk.

    Frozen + dict-of-strings keeps the structure trivially hashable; the
    manifest helpers below consume it directly without re-reading files.
    """

    playbook_common: str
    personas: dict[str, str]
    playbooks: dict[str, str]


def load_prompts(prompts_dir: str | Path) -> PromptParts:
    """Read the prompt content tree rooted at ``prompts_dir``.

    Expected layout::

        <prompts_dir>/
          playbook_common.md
          personas/<persona>.md
          playbooks/<persona>.md

    The persona name is the filename stem; the set of stems under
    ``personas/`` and ``playbooks/`` must match exactly. A mismatch raises a
    ``ValueError`` (loud failure beats a silent missing-persona at compose
    time).
    """
    root = Path(prompts_dir)
    common = (root / "playbook_common.md").read_text("utf-8")
    personas = {
        f.stem: f.read_text("utf-8") for f in sorted((root / "personas").glob("*.md"))
    }
    playbooks = {
        f.stem: f.read_text("utf-8") for f in sorted((root / "playbooks").glob("*.md"))
    }
    missing_playbook = set(personas) - set(playbooks)
    missing_persona = set(playbooks) - set(personas)
    if missing_playbook or missing_persona:
        raise ValueError(
            "persona/playbook filename stems must match: "
            f"missing playbook for {sorted(missing_playbook)}, "
            f"missing persona for {sorted(missing_persona)}"
        )
    return PromptParts(playbook_common=common, personas=personas, playbooks=playbooks)


# ---------------------------------------------------------------------------
# Compose pipeline
# ---------------------------------------------------------------------------


def compose(
    parts: PromptParts,
    *,
    persona: str,
    ablation: Ablation,
    output_contract_schema: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
    label_inference: LabelInference,
) -> str:
    """Assemble the system prompt for one ``(persona, ablation)``.

    Order::

        system_scaffold + output_contract + persona + playbook_common
            + playbook_<persona> + tool_schemas

    Under ``ablation="playbooks_off"`` the per-persona playbook section is
    omitted (persona + common playbook still apply). Under
    ``ablation="tools_off"`` the *caller* is expected to pass a reduced
    ``tool_schemas`` containing only ``submit_assessment``; this function
    just embeds whatever it is given.

    The assembled string is run through :func:`check_forbidden_tokens` before
    being returned, so a future content edit that introduces a forbidden token
    will fail at compose time rather than at run time.
    """
    if persona not in parts.personas:
        raise KeyError(
            f"unknown persona: {persona!r}; known: {sorted(parts.personas)}"
        )

    output_contract_block = json.dumps(output_contract_schema, indent=2, sort_keys=True)
    tool_schemas_block = json.dumps(tool_schemas, indent=2, sort_keys=True)

    sections: list[str] = [
        f"# System\n{SYSTEM_SCAFFOLD}",
        f"# Output Contract\n```json\n{output_contract_block}\n```",
        f"# Persona\n{parts.personas[persona]}",
        f"# Common Playbook\n{parts.playbook_common}",
    ]
    if ablation != "playbooks_off":
        sections.append(f"# Persona Playbook\n{parts.playbooks[persona]}")
    sections.append(f"# Tools\n```json\n{tool_schemas_block}\n```")

    assembled = "\n\n".join(sections)
    check_forbidden_tokens(
        assembled,
        label_inference=label_inference,
        where=f"composed[{persona},{ablation}]",
    )
    return assembled


# ---------------------------------------------------------------------------
# Manifest hashes
# ---------------------------------------------------------------------------


def prompts_manifest_sha(parts: PromptParts) -> str:
    """Hash of the prompt content that is invariant across ablations.

    Covers the system scaffold, the shared playbook (which persists under
    ``playbooks_off``), and every persona block. Does NOT include per-persona
    playbooks — those are tracked separately by
    :func:`playbooks_manifest_sha` so the ``playbooks_off`` ablation rotates
    one hash without disturbing the other.
    """
    return hash_obj(
        {
            "system_scaffold": SYSTEM_SCAFFOLD,
            "playbook_common": parts.playbook_common,
            "personas": dict(sorted(parts.personas.items())),
        }
    )


def playbooks_manifest_sha(parts: PromptParts, *, ablation: Ablation = "main") -> str:
    """Hash of per-persona playbooks AS APPLIED for this ablation.

    Under ``playbooks_off`` the playbooks are dropped and the hash collapses
    to the empty-playbooks fingerprint. This way the hash alone is enough to
    tell two runs apart without also consulting the ablation tag.
    """
    payload: dict[str, str] = (
        {} if ablation == "playbooks_off" else dict(sorted(parts.playbooks.items()))
    )
    return hash_obj({"playbooks": payload})


__all__ = [
    "SYSTEM_SCAFFOLD",
    "Ablation",
    "ForbiddenTokenInPrompt",
    "PromptParts",
    "check_forbidden_tokens",
    "compose",
    "load_prompts",
    "playbooks_manifest_sha",
    "prompts_manifest_sha",
]

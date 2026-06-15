"""Tool framework: abstract base class, context, and registry.

This module is the policy-free core of the tool layer. It defines:

- :class:`Tool`: abstract base every concrete tool subclasses.
- :class:`ToolContext`: read-only handle into a built index.
- :class:`ToolSchemaViolation`: raised when a tool call's args fail validation.
- :class:`ToolRegistry`: name -> tool lookup with persona-allowlist
  enforcement and a deterministic manifest hash.

It deliberately knows nothing about specific tools (see :mod:`socbench.tools.impl`)
or about diagnostics (see :mod:`socbench.tools.smoke`). Importing this module
has no side effects and triggers no other imports inside the package.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import jsonschema

from socbench.hashing import hash_obj

# ---------------------------------------------------------------------------
# Ground-truth safety net (tools are read-only views, but they MUST
# NOT surface any label-derived field to the model; that would let a trivial
# agent copy `is_malicious` into `malicious_flow_indices` and score F1=1.00
# without reasoning. Every tool response is recursively scanned at the
# boundary; a violation is a *programming* bug, not a user input issue.)
# ---------------------------------------------------------------------------

GROUND_TRUTH_FIELDS: frozenset[str] = frozenset(
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


class GroundTruthLeak(RuntimeError):
    """Raised when a tool response would expose a label-derived field.

    The fix is always to drop the offending column from the tool's SQL or
    response shaping; never to relax this check. The forbidden field set is
    deliberately a closed allowlist; expand it if the index grows new
    label-derived columns.
    """


def _assert_no_ground_truth_leak(payload: Any, *, path: str = "$") -> None:
    """Recursively walk a tool response and raise on any forbidden key.

    Walk order is deterministic; the raised path string locates the offender
    so the tool author can fix it directly. Lists and dicts are descended;
    leaves are ignored; only dict KEYS matter, never values.
    """
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k in GROUND_TRUTH_FIELDS:
                raise GroundTruthLeak(
                    f"tool response leaks ground-truth field at {path}.{k}; "
                    f"forbidden set: {sorted(GROUND_TRUTH_FIELDS)}"
                )
            _assert_no_ground_truth_leak(v, path=f"{path}.{k}")
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            _assert_no_ground_truth_leak(item, path=f"{path}[{i}]")


@dataclass(frozen=True)
class ToolContext:
    """Read-only handle into a built index. Constructed once per agent loop."""

    index_dir: Path
    max_results_cap: int = 200

    @property
    def flows_parquet(self) -> Path:
        return self.index_dir / "flows.parquet"

    @property
    def pairs_jsonl(self) -> Path:
        return self.index_dir / "pairs.jsonl"

    @property
    def hosts_jsonl(self) -> Path:
        return self.index_dir / "hosts.jsonl"

    @property
    def eval_units_jsonl(self) -> Path:
        return self.index_dir / "eval_units.jsonl"

    @property
    def hosts_rollup_parquet(self) -> Path:
        return self.index_dir / "rollups" / "hosts.parquet"

    @property
    def pair_stats_rollup_parquet(self) -> Path:
        return self.index_dir / "rollups" / "pair_stats.parquet"


class ToolSchemaViolation(Exception):
    """Raised when args fail the tool's input JSON Schema (recoverable)."""


class Tool(ABC):
    """Abstract base class for every callable tool.

    Implementations MUST be deterministic, in-process, and read-only.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    args_schema: ClassVar[dict[str, Any]]

    def schema_hash(self) -> str:
        return hash_obj({"name": self.name, "args_schema": self.args_schema})

    def validate_args(self, args: dict[str, Any]) -> None:
        try:
            jsonschema.validate(instance=args, schema=self.args_schema)
        except jsonschema.ValidationError as exc:
            raise ToolSchemaViolation(exc.message) from exc

    @abstractmethod
    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        raise NotImplementedError

    def __call__(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.validate_args(args)
        result = self.call(args, ctx)
        # Defensive: surface non-JSON-able payloads as bugs at the boundary.
        json.dumps(result)
        # Belt-and-suspenders: even if a future tool author forgets to strip
        # a label column from their SELECT, the response never reaches the
        # model with ground truth attached. Surgical SQL fixes per tool are
        # the primary defense; this is the safety net.
        _assert_no_ground_truth_leak(result)
        return result


class ToolRegistry:
    """Lookup-by-name + persona allowlist enforcement + manifest hashing.

    The persona allowlist comes from configuration (see
    ``socbench.config.AgentConfig.persona_tool_allowlist``), **not** from a
    constant in this module. That keeps the persona × tool matrix in a single
    place (``config/benchmark_config.yaml``) that every stage can read.

    If ``persona_allowlist`` is ``None``, persona-aware methods raise. Pass an
    allowlist whenever the agent loop, smoke runner, or prompts need
    persona-scoped behavior.
    """

    def __init__(
        self,
        tools: Iterable[Tool],
        *,
        persona_allowlist: dict[str, set[str]] | None = None,
    ):
        self._by_name: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._by_name:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._by_name[tool.name] = tool

        self._persona_allowlist: dict[str, frozenset[str]] = {}
        if persona_allowlist:
            registered = set(self._by_name)
            for persona, allowed in persona_allowlist.items():
                unknown = set(allowed) - registered
                if unknown:
                    raise ValueError(
                        f"persona {persona!r} allowlist references unknown tools: "
                        f"{sorted(unknown)}; registered tools: {sorted(registered)}"
                    )
                self._persona_allowlist[persona] = frozenset(allowed)

    def get(self, name: str) -> Tool:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def personas(self) -> list[str]:
        return sorted(self._persona_allowlist)

    def _require_persona(self, persona: str) -> frozenset[str]:
        try:
            return self._persona_allowlist[persona]
        except KeyError as exc:
            if not self._persona_allowlist:
                raise RuntimeError(
                    "registry has no persona allowlist; "
                    "construct it with persona_allowlist=cfg.agent.persona_tool_allowlist()"
                ) from exc
            raise KeyError(
                f"unknown persona: {persona}; known: {self.personas()}"
            ) from exc

    def tools_for_persona(self, persona: str) -> list[Tool]:
        allowed = self._require_persona(persona)
        return [self._by_name[n] for n in self.names() if n in allowed]

    def is_allowed(self, persona: str, tool_name: str) -> bool:
        return tool_name in self._require_persona(persona)

    def manifest_sha(self) -> str:
        """Stable hash of tool schemas + persona allowlist.

        Changing a tool's args schema *or* the persona × tool matrix bumps
        this hash, so run artifacts carry an honest fingerprint of the
        policy under which they were produced.
        """
        parts = {
            "tools": {name: self._by_name[name].args_schema for name in self.names()},
            "persona_allowlist": {
                p: sorted(s) for p, s in sorted(self._persona_allowlist.items())
            },
        }
        return hash_obj(parts)

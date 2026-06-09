"""The catalog of read-only tools shipped with socbench.

Each tool lives in its own module so that:

- adding a tool is a single-file PR (plus one line here and a YAML allowlist edit)
- ``git blame`` on a tool reads as the tool's history
- the filename matches the tool name used in ``config/benchmark_config.yaml``
  under ``agent.personas.<persona>.tools``, so navigation from policy to
  implementation is trivial

``ALL_TOOLS`` is the canonical registration list — order has no semantic
meaning. ``build_default_registry`` is the only factory callers should use;
it instantiates one of each tool and binds the persona allowlist supplied
by configuration.
"""
from __future__ import annotations

from socbench.tools.base import Tool, ToolRegistry
from socbench.tools.catalog.get_flows import GetFlowsTool
from socbench.tools.catalog.get_pair_timeline import GetPairTimelineTool
from socbench.tools.catalog.host_rollup import HostRollupTool
from socbench.tools.catalog.list_pairs import ListPairsTool
from socbench.tools.catalog.pair_stats import PairStatsTool
from socbench.tools.catalog.port_proto_matrix import PortProtoMatrixTool
from socbench.tools.catalog.rarity_stats import RarityStatsTool
from socbench.tools.catalog.submit_assessment import SubmitAssessmentTool
from socbench.tools.catalog.top_destinations import TopDestinationsTool

ALL_TOOLS: list[type[Tool]] = [
    ListPairsTool,
    GetPairTimelineTool,
    GetFlowsTool,
    HostRollupTool,
    TopDestinationsTool,
    PairStatsTool,
    PortProtoMatrixTool,
    RarityStatsTool,
    SubmitAssessmentTool,
]


def build_default_registry(
    *, persona_allowlist: dict[str, set[str]] | None = None
) -> ToolRegistry:
    """Return a registry seeded with every shipped tool.

    Lives next to ``ALL_TOOLS`` so the factory and the list it iterates
    travel together. Callers typically import it via ``socbench.tools``.
    """
    return ToolRegistry(
        tools=[cls() for cls in ALL_TOOLS],
        persona_allowlist=persona_allowlist,
    )


__all__ = [
    "ALL_TOOLS",
    "GetFlowsTool",
    "GetPairTimelineTool",
    "HostRollupTool",
    "ListPairsTool",
    "PairStatsTool",
    "PortProtoMatrixTool",
    "RarityStatsTool",
    "SubmitAssessmentTool",
    "TopDestinationsTool",
    "build_default_registry",
]

"""Public surface of the tool layer (re-exports only).

This package is split into single-responsibility modules:

- :mod:`socbench.tools.base`:    ``Tool`` ABC, ``ToolContext``,
  ``ToolRegistry``, ``ToolSchemaViolation``. The policy-free framework.
- :mod:`socbench.tools.catalog`: the catalog of nine concrete tools, each
  in its own module (e.g. ``catalog/list_pairs.py``), plus ``ALL_TOOLS`` and
  the ``build_default_registry`` factory.
- :mod:`socbench.tools.smoke`:   ``run_smoke``, used by ``socbench tools-smoke``
  and by the tool tests.

This ``__init__`` deliberately contains no functional code; it only re-exports
the public API so callers keep writing ``from socbench.tools import X``. The
persona x tool matrix is **not** defined anywhere in this package. It lives
in ``config/benchmark_config.yaml`` under ``agent.personas.<name>.tools`` and
is supplied to ``build_default_registry`` at construction time.
"""
from __future__ import annotations

from socbench.tools.base import (
    GROUND_TRUTH_FIELDS,
    GroundTruthLeak,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolSchemaViolation,
)
from socbench.tools.catalog import ALL_TOOLS, build_default_registry
from socbench.tools.smoke import run_smoke

__all__ = [
    "ALL_TOOLS",
    "GROUND_TRUTH_FIELDS",
    "GroundTruthLeak",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolSchemaViolation",
    "build_default_registry",
    "run_smoke",
]

"""socbench — frontier LLMs as SOC agents on raw NetFlow.

Benchmarks frontier reasoning models as SOC agents over a deterministic,
pre-indexed NetFlow corpus with persona-scoped read-only tools, bounded
multi-turn agent loops, and a strict final-answer contract.
"""
from socbench._version import __version__

__all__ = ["__version__"]

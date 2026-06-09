"""Shared internal helpers for catalog tools.

Private to ``socbench.tools.catalog``. Keeps the framework in
:mod:`socbench.tools.base` free of any query-engine dependency (DuckDB).
"""
from __future__ import annotations

from typing import Any

import duckdb


def _with_conn(fn):  # type: ignore[no-untyped-def]
    """Decorator that injects a fresh DuckDB connection as the first arg.

    A new connection per call keeps tools trivially thread-safe and free of
    cross-call state. Connection open + close is cheap (~µs).
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        con = duckdb.connect()
        try:
            return fn(con, *args, **kwargs)
        finally:
            con.close()

    return wrapper


def _cap_limit(requested: int | None, default: int, hard_cap: int) -> int:
    """Clamp a tool-side ``limit`` argument so models can't blow up prompt size."""
    if requested is None:
        requested = default
    return max(1, min(int(requested), hard_cap))

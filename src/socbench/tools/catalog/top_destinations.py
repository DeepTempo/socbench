"""``top_destinations`` — the top destinations a host talked to.

Persona allowlist: Threat, Hunter, DE.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext
from socbench.tools.catalog._helpers import _cap_limit, _with_conn


class TopDestinationsTool(Tool):
    name: ClassVar[str] = "top_destinations"
    description: ClassVar[str] = (
        "Return the top destinations a host talked to, ranked by flow count, "
        "with byte totals and time window per destination."
    )
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["host"],
        "properties": {
            "host": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        },
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return _query(
            ctx=ctx, host=args["host"], limit=_cap_limit(args.get("limit"), 10, 100)
        )


@_with_conn
def _query(  # type: ignore[no-untyped-def]
    con, *, ctx: ToolContext, host: str, limit: int
) -> dict[str, Any]:
    sql = f"""
        SELECT dst_ip, flow_count, bytes_total,
               pkts_total, distinct_dst_ports, distinct_protocols,
               ts_start_min, ts_start_max
        FROM read_parquet('{ctx.pair_stats_rollup_parquet}')
        WHERE src_ip = ?
        ORDER BY flow_count DESC
        LIMIT {limit}
    """
    rows = con.execute(sql, [host]).fetchall()
    cols = [d[0] for d in con.description]
    items = [dict(zip(cols, r, strict=True)) for r in rows]
    return {"host": host, "items": items, "limit": limit, "returned": len(items)}

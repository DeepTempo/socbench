"""``list_pairs``: enumerate (src_ip, dst_ip) pairs with summary stats.

Persona allowlist: SOC, Threat, Hunter, DE.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext
from socbench.tools.catalog._helpers import _cap_limit, _with_conn

_LIST_PAIRS_SORT = {
    "flow_count": "flow_count DESC",
    "bytes_total": "bytes_total DESC",
    "pkts_total": "pkts_total DESC",
    "distinct_dst_ports": "distinct_dst_ports DESC",
    "ts_start": "ts_start_min ASC",
}


class ListPairsTool(Tool):
    name: ClassVar[str] = "list_pairs"
    description: ClassVar[str] = (
        "List (src_ip, dst_ip) pairs from the corpus, with summary stats. "
        "Supports optional src_ip / dst_ip filters and a sort key."
    )
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "src_ip": {"type": "string"},
            "dst_ip": {"type": "string"},
            "sort": {"type": "string", "enum": sorted(_LIST_PAIRS_SORT)},
            "limit": {"type": "integer", "minimum": 1},
        },
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        limit = _cap_limit(args.get("limit"), 20, ctx.max_results_cap or 200)
        sort_sql = _LIST_PAIRS_SORT[args.get("sort", "flow_count")]
        clauses: list[str] = []
        params: list[Any] = []
        if "src_ip" in args:
            clauses.append("src_ip = ?")
            params.append(args["src_ip"])
        if "dst_ip" in args:
            clauses.append("dst_ip = ?")
            params.append(args["dst_ip"])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return _query(ctx=ctx, sort_sql=sort_sql, where=where, params=params, limit=limit)


@_with_conn
def _query(  # type: ignore[no-untyped-def]
    con, *, ctx: ToolContext, sort_sql: str, where: str, params: list[Any], limit: int
) -> dict[str, Any]:
    sql = f"""
        SELECT src_ip, dst_ip, flow_count,
               ts_start_min, ts_start_max, bytes_total, pkts_total,
               distinct_dst_ports, distinct_src_ports, distinct_protocols
        FROM read_parquet('{ctx.pair_stats_rollup_parquet}')
        {where}
        ORDER BY {sort_sql}
        LIMIT {limit}
    """
    rows = con.execute(sql, params).fetchall()
    cols = [d[0] for d in con.description]
    items = [dict(zip(cols, r, strict=True)) for r in rows]
    return {"items": items, "limit": limit, "returned": len(items)}

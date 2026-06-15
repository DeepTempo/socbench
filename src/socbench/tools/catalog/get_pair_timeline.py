"""``get_pair_timeline`` — time-ordered flow records for one (src_ip, dst_ip) pair.

Persona allowlist: SOC, Threat, Hunter, DE.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext
from socbench.tools.catalog._helpers import _cap_limit, _with_conn


class GetPairTimelineTool(Tool):
    name: ClassVar[str] = "get_pair_timeline"
    description: ClassVar[str] = (
        "Return time-ordered flow records for one (src_ip, dst_ip) pair, "
        "supporting offset and limit."
    )
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["src_ip", "dst_ip"],
        "properties": {
            "src_ip": {"type": "string"},
            "dst_ip": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1},
        },
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        limit = _cap_limit(args.get("limit"), 50, 500)
        offset = int(args.get("offset", 0))
        return _query(
            ctx=ctx,
            src_ip=args["src_ip"],
            dst_ip=args["dst_ip"],
            offset=offset,
            limit=limit,
        )


@_with_conn
def _query(  # type: ignore[no-untyped-def]
    con, *, ctx: ToolContext, src_ip: str, dst_ip: str, offset: int, limit: int
) -> dict[str, Any]:
    sql = f"""
        SELECT _flow_id AS flow_id, ts_start, protocol, src_port, dst_port,
               bytes_in, bytes_out, pkts_in, pkts_out, tcp_flags,
               flow_duration_ms
        FROM read_parquet('{ctx.flows_parquet}')
        WHERE src_ip = ? AND dst_ip = ?
        ORDER BY ts_start, _flow_id
        LIMIT {limit} OFFSET {offset}
    """
    rows = con.execute(sql, [src_ip, dst_ip]).fetchall()
    cols = [d[0] for d in con.description]
    items = [dict(zip(cols, r, strict=True)) for r in rows]
    total = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{ctx.flows_parquet}') "
        f"WHERE src_ip = ? AND dst_ip = ?",
        [src_ip, dst_ip],
    ).fetchone()
    return {
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "items": items,
        "offset": offset,
        "limit": limit,
        "returned": len(items),
        "total_flows_in_pair": int(total[0]) if total else 0,
    }

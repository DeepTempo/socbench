"""``get_flows`` — fetch flow records by explicit flow_id list.

Persona allowlist: SOC, Threat, Hunter, DE.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext
from socbench.tools.catalog._helpers import _with_conn


class GetFlowsTool(Tool):
    name: ClassVar[str] = "get_flows"
    description: ClassVar[str] = "Fetch flow records by explicit flow_id list."
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["flow_ids"],
        "properties": {
            "flow_ids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "minItems": 1,
                "maxItems": 500,
            }
        },
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        ids = [int(x) for x in args["flow_ids"]][:500]
        return _query(ctx=ctx, ids=ids)


@_with_conn
def _query(con, *, ctx: ToolContext, ids: list[int]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    placeholder = ",".join("?" * len(ids))
    sql = f"""
        SELECT _flow_id AS flow_id, src_ip, dst_ip, ts_start, protocol,
               src_port, dst_port, bytes_in, bytes_out, pkts_in, pkts_out,
               tcp_flags, flow_duration_ms
        FROM read_parquet('{ctx.flows_parquet}')
        WHERE _flow_id IN ({placeholder})
        ORDER BY _flow_id
    """
    rows = con.execute(sql, ids).fetchall()
    cols = [d[0] for d in con.description]
    items = [dict(zip(cols, r, strict=True)) for r in rows]
    found = {it["flow_id"] for it in items}
    missing = [i for i in ids if i not in found]
    return {
        "items": items,
        "requested": len(ids),
        "returned": len(items),
        "missing_flow_ids": missing,
    }

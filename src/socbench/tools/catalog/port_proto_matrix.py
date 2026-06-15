"""``port_proto_matrix``: (dst_port, protocol) breakdown for a scope.

Persona allowlist: Hunter, DE.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext
from socbench.tools.catalog._helpers import _cap_limit, _with_conn


class PortProtoMatrixTool(Tool):
    name: ClassVar[str] = "port_proto_matrix"
    description: ClassVar[str] = (
        "Flow count and bytes broken down by (dst_port, protocol) for an "
        "optional scope (host or pair). Useful for shape-of-traffic analysis."
    )
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scope": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "src_ip": {"type": "string"},
                    "dst_ip": {"type": "string"},
                },
            },
            "limit": {"type": "integer", "minimum": 1},
        },
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return _query(
            ctx=ctx,
            scope=args.get("scope") or {},
            limit=_cap_limit(args.get("limit"), 50, 500),
        )


@_with_conn
def _query(  # type: ignore[no-untyped-def]
    con, *, ctx: ToolContext, scope: dict[str, Any], limit: int
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if "src_ip" in scope:
        clauses.append("src_ip = ?")
        params.append(scope["src_ip"])
    if "dst_ip" in scope:
        clauses.append("dst_ip = ?")
        params.append(scope["dst_ip"])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        SELECT dst_port, protocol,
               COUNT(*) AS flow_count,
               SUM(bytes_in + bytes_out) AS bytes_total
        FROM read_parquet('{ctx.flows_parquet}')
        {where}
        GROUP BY dst_port, protocol
        ORDER BY flow_count DESC, dst_port ASC
        LIMIT {limit}
    """
    rows = con.execute(sql, params).fetchall()
    cols = [d[0] for d in con.description]
    items = [dict(zip(cols, r, strict=True)) for r in rows]
    return {"scope": scope, "items": items, "limit": limit, "returned": len(items)}

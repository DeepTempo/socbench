"""``rarity_stats``: frequency tail of dst_port and dst_ip across a scope.

Persona allowlist: Hunter, DE. Useful for hunting beacons and
uncommon egress.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext
from socbench.tools.catalog._helpers import _cap_limit, _with_conn


class RarityStatsTool(Tool):
    name: ClassVar[str] = "rarity_stats"
    description: ClassVar[str] = (
        "Frequency tail of dst_port and dst_ip across a scope: the rarest "
        "destinations and ports, useful for hunting beacons and uncommon egress."
    )
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scope": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"src_ip": {"type": "string"}},
            },
            "limit": {"type": "integer", "minimum": 1},
        },
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        scope = args.get("scope") or {}
        limit = _cap_limit(args.get("limit"), 20, 200)
        return {
            "scope": scope,
            "rarest_dst_ports": _rarest(ctx, scope, col="dst_port", limit=limit),
            "rarest_dst_ips": _rarest(ctx, scope, col="dst_ip", limit=limit),
        }


@_with_conn
def _rarest(  # type: ignore[no-untyped-def]
    con, ctx: ToolContext, scope: dict[str, Any], *, col: str, limit: int
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if "src_ip" in scope:
        where = "WHERE src_ip = ?"
        params.append(scope["src_ip"])
    sql = f"""
        SELECT {col} AS value, COUNT(*) AS flow_count
        FROM read_parquet('{ctx.flows_parquet}')
        {where}
        GROUP BY {col}
        ORDER BY flow_count ASC, value ASC
        LIMIT {limit}
    """
    rows = con.execute(sql, params).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]

"""``pair_stats`` — rollup stats for one (src_ip, dst_ip) pair.

Persona allowlist: Threat, Hunter, DE.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext
from socbench.tools.catalog._helpers import _with_conn


class PairStatsTool(Tool):
    name: ClassVar[str] = "pair_stats"
    description: ClassVar[str] = (
        "Return pre-computed rollup stats for one (src_ip, dst_ip) pair: "
        "flow counts, byte / packet totals, distinct ports / protocols, "
        "time window."
    )
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["src_ip", "dst_ip"],
        "properties": {
            "src_ip": {"type": "string"},
            "dst_ip": {"type": "string"},
        },
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return _query(ctx=ctx, src_ip=args["src_ip"], dst_ip=args["dst_ip"])


@_with_conn
def _query(  # type: ignore[no-untyped-def]
    con, *, ctx: ToolContext, src_ip: str, dst_ip: str
) -> dict[str, Any]:
    # Explicit safe-column list (no SELECT *): the rollup also stores
    # malicious_flow_count, which must not surface to the model.
    sql = f"""
        SELECT flow_count, bytes_total, pkts_total,
               distinct_dst_ports, distinct_src_ports, distinct_protocols,
               ts_start_min, ts_start_max
        FROM read_parquet('{ctx.pair_stats_rollup_parquet}')
        WHERE src_ip = ? AND dst_ip = ?
    """
    rows = con.execute(sql, [src_ip, dst_ip]).fetchall()
    if not rows:
        return {"src_ip": src_ip, "dst_ip": dst_ip, "found": False}
    cols = [d[0] for d in con.description]
    return {
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "found": True,
        "stats": dict(zip(cols, rows[0], strict=True)),
    }

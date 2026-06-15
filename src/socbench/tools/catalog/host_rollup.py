"""``host_rollup``: pre-computed rollup stats for a single host.

Persona allowlist: SOC, Threat, Hunter, DE.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext
from socbench.tools.catalog._helpers import _with_conn


class HostRollupTool(Tool):
    name: ClassVar[str] = "host_rollup"
    description: ClassVar[str] = (
        "Return pre-computed rollup stats for a single host (src_ip): flow / "
        "destination counts, byte / packet totals, time window, distinct ports."
    )
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["host"],
        "properties": {"host": {"type": "string"}},
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return _query(ctx=ctx, host=args["host"])


@_with_conn
def _query(con, *, ctx: ToolContext, host: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    # Explicit safe-column list (no SELECT *): the rollup also stores
    # malicious_flow_count and distinct_malicious_destinations, which must
    # not surface to the model.
    sql = f"""
        SELECT flow_count, distinct_destinations,
               bytes_out_total, bytes_in_total,
               pkts_out_total, pkts_in_total,
               ts_start_min, ts_start_max,
               distinct_dst_ports, distinct_protocols
        FROM read_parquet('{ctx.hosts_rollup_parquet}')
        WHERE host = ?
    """
    rows = con.execute(sql, [host]).fetchall()
    if not rows:
        return {"host": host, "found": False}
    cols = [d[0] for d in con.description]
    return {"host": host, "found": True, "stats": dict(zip(cols, rows[0], strict=True))}

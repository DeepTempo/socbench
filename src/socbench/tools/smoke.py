"""End-to-end smoke runner for the tool layer.

Walks every tool a persona is allowed to call and records the result. Used by:

- ``socbench tools-smoke`` (Stage-3 diagnostic; verifies an index is queryable)
- ``tests/test_tools.py`` (CI guarantee that every tool's happy path works)

Implementation is intentionally tolerant: a tool that raises is recorded but
doesn't halt the run, so a partial regression still produces a full diff.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from socbench.tools.base import ToolContext, ToolRegistry, ToolSchemaViolation


def _pick_seed_pair(index_dir: Path) -> tuple[str, str] | None:
    pair_stats = index_dir / "rollups" / "pair_stats.parquet"
    if not pair_stats.exists():
        return None
    con = duckdb.connect()
    try:
        row = con.execute(
            f"SELECT src_ip, dst_ip FROM read_parquet('{pair_stats}') "
            f"ORDER BY flow_count DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return (row[0], row[1]) if row else None


def _pick_seed_flow_ids(index_dir: Path, limit: int = 5) -> list[int]:
    flows = index_dir / "flows.parquet"
    if not flows.exists():
        return []
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT _flow_id FROM read_parquet('{flows}') "
            f"ORDER BY _flow_id LIMIT {limit}"
        ).fetchall()
    finally:
        con.close()
    return [int(r[0]) for r in rows]


def _default_args(
    tool_name: str, seed_pair: tuple[str, str] | None, seed_ids: list[int]
) -> dict[str, Any]:
    src_ip, dst_ip = seed_pair if seed_pair else ("0.0.0.0", "0.0.0.0")
    defaults: dict[str, dict[str, Any]] = {
        "list_pairs": {"sort": "flow_count", "limit": 5},
        "get_pair_timeline": {"src_ip": src_ip, "dst_ip": dst_ip, "limit": 5},
        "get_flows": {"flow_ids": seed_ids or [0]},
        "host_rollup": {"host": src_ip},
        "top_destinations": {"host": src_ip, "limit": 5},
        "pair_stats": {"src_ip": src_ip, "dst_ip": dst_ip},
        "port_proto_matrix": {"scope": {"src_ip": src_ip}, "limit": 5},
        "rarity_stats": {"scope": {"src_ip": src_ip}, "limit": 5},
        "submit_assessment": {
            "verdict": "benign",
            "confidence": 0.5,
            "malicious_flow_indices": [],
            "rationale": "smoke-only invocation; no agent loop attached.",
        },
    }
    try:
        return defaults[tool_name]
    except KeyError as exc:
        raise KeyError(f"no default args for tool {tool_name!r}") from exc


def _summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, list):
            summary[k] = {"type": "list", "length": len(v)}
        elif isinstance(v, dict):
            summary[k] = {"type": "dict", "keys": sorted(v)[:8]}
        else:
            summary[k] = v
    return summary


def run_smoke(*, registry: ToolRegistry, persona: str, index_dir: Path) -> dict[str, Any]:
    """Run every persona-allowed tool once and collect summaries."""
    ctx = ToolContext(index_dir=index_dir)
    seed_pair = _pick_seed_pair(index_dir)
    seed_ids = _pick_seed_flow_ids(index_dir)

    results: dict[str, Any] = {
        "persona": persona,
        "index_dir": str(index_dir),
        "tools_manifest_sha": registry.manifest_sha(),
        "seed_pair": list(seed_pair) if seed_pair else None,
        "seed_flow_ids": seed_ids,
        "tools": {},
    }

    for tool in registry.tools_for_persona(persona):
        args = _default_args(tool.name, seed_pair, seed_ids)
        entry: dict[str, Any] = {"args": args, "ok": False}
        try:
            payload = tool(args, ctx)
            entry["ok"] = True
            entry["summary"] = _summarize_payload(payload)
        except ToolSchemaViolation as exc:
            entry["error_type"] = "schema_violation"
            entry["error"] = str(exc)
        except Exception as exc:  # surface any runtime issue without halting the run
            entry["error_type"] = type(exc).__name__
            entry["error"] = str(exc)
        results["tools"][tool.name] = entry
    return results

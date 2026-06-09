"""Config loaders are strict and reject typos."""
from __future__ import annotations

from pathlib import Path

import pytest

from socbench.config import load_benchmark_config, load_pricing

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


def test_benchmark_config_loads() -> None:
    cfg = load_benchmark_config(CONFIG_DIR / "benchmark_config.yaml")
    assert cfg.index.host_egress_fanout_K == 10
    assert cfg.index.host_egress_window_minutes == 5
    assert cfg.index.max_flows_per_unit == 1000
    assert cfg.agent.cost_usd_cap_per_rendering == 0.50
    assert "sample" in cfg.datasets
    for persona in ("soc_analyst", "threat_analyst", "adversary_hunter", "detection_engineer"):
        assert persona in cfg.agent.personas


def test_persona_policy_includes_tools_and_allowlist_helper() -> None:
    cfg = load_benchmark_config(CONFIG_DIR / "benchmark_config.yaml")

    soc = cfg.agent.personas["soc_analyst"]
    assert soc.max_turns >= 1
    assert "submit_assessment" in soc.tools, "every persona must be able to finalize"
    assert "list_pairs" in soc.tools

    allowlist = cfg.agent.persona_tool_allowlist()
    assert set(allowlist) == set(cfg.agent.personas)
    assert all("submit_assessment" in tools for tools in allowlist.values())
    assert allowlist["soc_analyst"] < allowlist["adversary_hunter"], (
        "wider personas must be a superset of narrower ones"
    )


def test_pricing_loads_and_computes_cost() -> None:
    pt = load_pricing(CONFIG_DIR / "pricing.yaml")
    cost = pt.cost_usd(
        provider="openai",
        model="gpt-5.5",
        prompt_tokens=10_000,
        cached_tokens=90_000,
        output_tokens=1_000,
        reasoning_tokens=0,
    )
    # 10k * 5/1M + 90k * 0.5/1M + 1k * 30/1M = 0.05 + 0.045 + 0.030 = 0.125
    assert pytest.approx(cost, rel=1e-6) == 0.125


def test_pricing_unknown_model_raises() -> None:
    pt = load_pricing(CONFIG_DIR / "pricing.yaml")
    with pytest.raises(KeyError):
        pt.cost_usd(
            provider="openai", model="not-a-real-model",
            prompt_tokens=1, cached_tokens=0, output_tokens=1, reasoning_tokens=0,
        )

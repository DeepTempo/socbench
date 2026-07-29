"""Typed loaders for ``config/benchmark_config.yaml`` and ``config/pricing.yaml``.

Unknown keys raise: config typos should fail loudly rather than silently
degrade a run.

Sibling-file paths inside ``benchmark_config.yaml`` (``schema_path``,
``pricing_path``) are resolved relative to the config file's directory, so
moving or renaming the ``config/`` folder requires no code edits.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Common type aliases
# ---------------------------------------------------------------------------

PersonaName = Literal["soc_analyst", "threat_analyst", "adversary_hunter", "detection_engineer"]
ProviderName = Literal["openai", "anthropic", "gemini", "open_source"]
LabelGroup = Literal["benign", "malicious"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# benchmark_config.yaml
# ---------------------------------------------------------------------------


class PathsConfig(_Strict):
    index_root: Path = Path("indexes")
    runs_root: Path = Path("runs")
    ablations_root: Path = Path("ablations")
    cache_dir: Path = Path(".cache")


class IndexConfig(_Strict):
    host_egress_fanout_K: Annotated[int, Field(ge=2)] = 10
    host_egress_window_minutes: Annotated[int, Field(ge=1)] = 5
    # Max flows per eval unit; larger units are split into contiguous time
    # sub-windows so each stays tractable under a finite model context window.
    max_flows_per_unit: Annotated[int, Field(ge=1)] = 1000
    rendering_caps_per_provider: dict[str, int] = Field(
        default_factory=lambda: {"claude": 600, "gpt": 1200, "gemini": 3000}
    )


class PersonaPolicy(_Strict):
    """Per-persona loop budget **and** tool allowlist.

    ``tools`` is the canonical persona × tool matrix; the tool registry reads
    it at construction time and rejects calls to non-listed tools. Names must
    match registered tools or registry construction fails fast.
    """

    max_turns: Annotated[int, Field(ge=1)]
    max_tool_calls: Annotated[int, Field(ge=1)]
    wall_clock_seconds: Annotated[int, Field(ge=1)]
    tools: Annotated[list[str], Field(min_length=1)]

    @model_validator(mode="after")
    def _tools_unique(self) -> PersonaPolicy:
        if len(set(self.tools)) != len(self.tools):
            raise ValueError(f"persona tools list contains duplicates: {self.tools}")
        return self


# Backwards-compatible alias: the old name was just about budgets.
PersonaBudget = PersonaPolicy


class AgentConfig(_Strict):
    cost_usd_cap_per_rendering: Annotated[float, Field(gt=0)] = 0.50
    personas: dict[PersonaName, PersonaPolicy]

    @model_validator(mode="after")
    def _require_all_personas(self) -> AgentConfig:
        required = {"soc_analyst", "threat_analyst", "adversary_hunter", "detection_engineer"}
        missing = required - set(self.personas)
        if missing:
            raise ValueError(f"agent.personas missing entries: {sorted(missing)}")
        return self

    def persona_tool_allowlist(self) -> dict[str, set[str]]:
        """Return ``{persona_name: set_of_tool_names}`` for registry construction."""
        return {name: set(policy.tools) for name, policy in self.personas.items()}


class SamplingModeConfig(_Strict):
    units_per_stratum: Annotated[int, Field(ge=1)]
    min_total_units: Annotated[int, Field(ge=1)] | None = None
    full_unit_cap: Annotated[int, Field(ge=1)] | None = None
    cost_budget_usd: Annotated[float, Field(gt=0)]


class SamplingConfig(_Strict):
    sample_seed: int = 7
    smoke: SamplingModeConfig
    full: SamplingModeConfig


class ProviderConfig(_Strict):
    enabled: bool = False
    model: str
    max_output_tokens: Annotated[int, Field(ge=1)] = 2048
    timeout_seconds: Annotated[int, Field(ge=1)] = 60
    max_retries: Annotated[int, Field(ge=0)] = 3
    max_concurrency: Annotated[int, Field(ge=1)] = 2
    # Scales this provider's per-rendering loop budget (max_turns, max_tool_calls,
    # wall_clock_seconds) AND the per-rendering cost cap. Reasoning-heavy providers
    # that take more tool-calling turns to reach a verdict (e.g. OpenAI on large
    # host_egress units) need >1.0 so they aren't forced into invalid submissions.
    budget_multiplier: Annotated[float, Field(ge=1.0)] = 1.0
    # After this many *consecutive* fatal renderings, the Runner stops
    # submitting new work for this provider (a hard-down / quota-exhausted
    # provider shouldn't burn the whole wall-clock). 0 disables the breaker.
    circuit_breaker_threshold: Annotated[int, Field(ge=0)] = 12
    # Optional sampling temperature. Left unset (None) by default because the
    # pinned frontier reasoning models reject it; set a value only for models
    # that still accept one.
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None


class DatasetEntry(_Strict):
    paths: list[Path]
    label_group: LabelGroup


class BenchmarkConfig(_Strict):
    schema_path: Path = Path("schema.json")
    pricing_path: Path = Path("pricing.yaml")
    paths: PathsConfig = PathsConfig()
    index: IndexConfig = IndexConfig()
    agent: AgentConfig
    sampling: SamplingConfig
    providers: dict[ProviderName, ProviderConfig]
    datasets: dict[str, DatasetEntry]


def load_benchmark_config(path: str | Path) -> BenchmarkConfig:
    """Load and validate ``benchmark_config.yaml``.

    Sibling-file paths (``schema_path``, ``pricing_path``) are resolved
    **relative to the config file's directory** when they're written as
    relative paths in the YAML. This keeps the ``config/`` folder
    self-contained, so moving or renaming it doesn't require code edits.

    Working-artifact paths (``paths.index_root`` / ``runs_root`` / etc., plus
    ``datasets[*].paths``) stay cwd-relative since they refer to outputs and
    inputs that conventionally live next to where you invoked the CLI.
    """
    config_file = Path(path).resolve()
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    cfg = BenchmarkConfig.model_validate(raw)

    config_dir = config_file.parent
    resolved = cfg.model_copy(
        update={
            "schema_path": _resolve_sibling(cfg.schema_path, config_dir),
            "pricing_path": _resolve_sibling(cfg.pricing_path, config_dir),
        }
    )
    return resolved


def _resolve_sibling(p: Path, config_dir: Path) -> Path:
    """Resolve ``p`` against ``config_dir`` if relative; otherwise return as-is."""
    return p if p.is_absolute() else (config_dir / p)


# ---------------------------------------------------------------------------
# pricing.yaml  ─ USD per 1,000,000 tokens.
# ---------------------------------------------------------------------------


class ModelPricing(_Strict):
    input: Annotated[float, Field(ge=0)]
    cached_input: Annotated[float, Field(ge=0)]
    output: Annotated[float, Field(ge=0)]
    reasoning: Annotated[float, Field(ge=0)]


class PricingTable(_Strict):
    snapshot_date: date
    currency: str = "USD"
    providers: dict[str, dict[str, ModelPricing]]

    def rate(self, provider: str, model: str) -> ModelPricing:
        try:
            return self.providers[provider][model]
        except KeyError as exc:
            raise KeyError(
                f"no pricing for provider={provider!r} model={model!r}; "
                f"add it to pricing.yaml or pick a priced model"
            ) from exc

    def cost_usd(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> float:
        """Compute USD cost for one call from per-million rates.

        ``prompt_tokens`` is the **uncached** input portion. ``cached_tokens``
        is reported separately by the provider and rated at ``cached_input``.
        """
        r = self.rate(provider, model)
        per_token = 1_000_000.0
        return (
            (prompt_tokens / per_token) * r.input
            + (cached_tokens / per_token) * r.cached_input
            + (output_tokens / per_token) * r.output
            + (reasoning_tokens / per_token) * r.reasoning
        )


def load_pricing(path: str | Path) -> PricingTable:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return PricingTable.model_validate(raw)


__all__ = [
    "AgentConfig",
    "BenchmarkConfig",
    "DatasetEntry",
    "IndexConfig",
    "ModelPricing",
    "PathsConfig",
    "PersonaBudget",
    "PersonaPolicy",
    "PricingTable",
    "ProviderConfig",
    "SamplingConfig",
    "SamplingModeConfig",
    "load_benchmark_config",
    "load_pricing",
]

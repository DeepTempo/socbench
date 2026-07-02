"""Single-file CLI: ``socbench {build-index, run, tools-smoke, aggregate}``.

Wired via the entry point ``socbench = "socbench.cli:cli"``. Each subcommand
delegates to library code in the matching module; this file is just argument
plumbing and structured-logging setup.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import click

from socbench._version import __version__
from socbench.agent import (
    RunConfig,
    Runner,
    generate_run_id,
    load_eval_units,
    select_eval_units,
)
from socbench.aggregate import aggregate_ablations
from socbench.config import load_benchmark_config, load_pricing
from socbench.index import build_index_for_dataset
from socbench.logging_config import configure_logging, get_logger
from socbench.prompts import load_prompts
from socbench.providers import build_adapter
from socbench.providers.base import FatalAdapterError
from socbench.sampling import stratified_sample
from socbench.schema import load_schema
from socbench.tools import ToolContext, build_default_registry, run_smoke

log = get_logger(__name__)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="socbench")
@click.option(
    "--log-level",
    default="INFO",
    show_default=True,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Root log level. Also reads SOCBENCH_LOG_LEVEL.",
)
@click.option(
    "--log-format",
    default=None,
    type=click.Choice(["json", "human"], case_sensitive=False),
    help="Log format. Defaults to SOCBENCH_LOG_FORMAT or 'json'.",
)
def cli(log_level: str, log_format: str | None) -> None:
    """socbench — frontier LLMs as SOC agents on raw NetFlow.

    See `socbench <subcommand> --help` for details.
    """
    configure_logging(level=log_level.upper(), fmt=log_format.lower() if log_format else None)


# ---------------------------------------------------------------------------
# build-index  (Step A)
# ---------------------------------------------------------------------------


@cli.command("build-index")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config/benchmark_config.yaml"),
    show_default=True,
)
@click.option("--dataset", "dataset_name", required=True)
@click.option(
    "--rebuild",
    is_flag=True,
    default=False,
    help="Force rebuild even if an index for this dataset_hash already exists.",
)
def build_index_cmd(config_path: Path, dataset_name: str, rebuild: bool) -> None:
    """Build a deterministic, content-addressed corpus index from parquet input."""
    cfg = load_benchmark_config(config_path)
    if dataset_name not in cfg.datasets:
        raise click.ClickException(
            f"dataset {dataset_name!r} not in config. Known: {sorted(cfg.datasets)}"
        )
    schema = load_schema(cfg.schema_path)
    result = build_index_for_dataset(
        dataset_name=dataset_name,
        dataset=cfg.datasets[dataset_name],
        schema=schema,
        index_cfg=cfg.index,
        index_root=cfg.paths.index_root,
        rebuild=rebuild,
    )
    click.echo(
        f"dataset_hash={result.dataset_hash} "
        f"flows={result.flow_count} "
        f"pairs={result.pair_count} "
        f"hosts={result.host_count} "
        f"eval_units={result.eval_unit_count} "
        f"path={result.index_dir}"
    )


# ---------------------------------------------------------------------------
# tools-smoke  (Step 3)
# ---------------------------------------------------------------------------


@cli.command("tools-smoke")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config/benchmark_config.yaml"),
    show_default=True,
)
@click.option(
    "--dataset-hash",
    required=True,
    help="The dataset_hash whose index/<dataset_hash>/ tree the tools should query.",
)
@click.option(
    "--persona",
    default="soc_analyst",
    show_default=True,
    help=(
        "Persona whose tool allowlist should be exercised. Validated against "
        "agent.personas in the benchmark config."
    ),
)
def tools_smoke_cmd(config_path: Path, dataset_hash: str, persona: str) -> None:
    """Invoke every tool available to PERSONA against the built index."""
    cfg = load_benchmark_config(config_path)
    if persona not in cfg.agent.personas:
        raise click.ClickException(
            f"unknown persona {persona!r}; known: {sorted(cfg.agent.personas)}"
        )
    index_dir = cfg.paths.index_root / dataset_hash
    if not index_dir.exists():
        raise click.ClickException(
            f"index dir {index_dir} not found — run `socbench build-index` first."
        )
    registry = build_default_registry(
        persona_allowlist=cfg.agent.persona_tool_allowlist()
    )
    result = run_smoke(registry=registry, persona=persona, index_dir=index_dir)
    click.echo(json.dumps(result, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# run  (Step 5)
# ---------------------------------------------------------------------------


_PROMPTS_DIR_DEFAULT = Path("config/prompts")


def _resolve_providers(raw: str, cfg_provider_keys: list[str]) -> list[str]:
    """Translate ``--providers`` into a concrete list.

    - ``"all"``  → every provider where ``cfg.providers[name].enabled == true``,
      plus ``"mock"`` if explicitly enabled there (mock is otherwise opt-in
      because it doesn't represent a real measurement).
    - ``"mock"`` (alone) → just the mock provider; convenient for dev / CI.
    - CSV like ``"openai,anthropic"`` → exactly those names.
    """
    raw = raw.strip()
    if raw == "all":
        if not cfg_provider_keys:
            raise click.ClickException(
                "--providers all selected, but no providers are enabled in config"
            )
        return cfg_provider_keys
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        raise click.ClickException("--providers cannot be empty")
    return names


@cli.command("run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config/benchmark_config.yaml"),
    show_default=True,
)
@click.option(
    "--prompts-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=_PROMPTS_DIR_DEFAULT,
    show_default=True,
    help="Directory containing playbook_common.md, personas/, playbooks/.",
)
@click.option("--dataset-hash", required=True, help="Index dataset_hash to query.")
@click.option(
    "--mode",
    type=click.Choice(["smoke", "full"], case_sensitive=False),
    default="smoke",
    show_default=True,
)
@click.option(
    "--ablation",
    type=click.Choice(["main", "tools_off", "playbooks_off"], case_sensitive=False),
    default="main",
    show_default=True,
    help="single_shot_baseline is the external single-shot run and not runnable here.",
)
@click.option(
    "--providers",
    "providers_raw",
    default="mock",
    show_default=True,
    help="'all' (every enabled in config) or CSV like 'openai,anthropic' or 'mock'.",
)
@click.option(
    "--personas",
    "personas_raw",
    default="all",
    show_default=True,
    help="'all' or CSV like 'soc_analyst,threat_analyst'.",
)
@click.option(
    "--unit-id",
    default=None,
    help="Override sampling: run exactly one eval unit by id (excludes --limit).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Override sampling: run the first N eval units by sorted eval_unit_id.",
)
@click.option(
    "--cost-budget-usd",
    type=float,
    default=None,
    help="Override cost_budget_usd from config. Run aborts cleanly when reached.",
)
@click.option(
    "--explicit-tool-use-prompt",
    is_flag=True,
    default=False,
    help="Use the explicit 'actually call tools, don't narrate' scaffold (sec8 shim). "
    "Default off → neutral scaffold for capable models.",
)
@click.option(
    "--context-window-tokens",
    type=int,
    default=None,
    help="Served context window (max_model_len) for self-hosted models. Set it so the "
    "loop force-submits before the transcript overflows and the server 400s. Omit for "
    "hosted APIs (huge native window).",
)
def run_cmd(
    config_path: Path,
    prompts_dir: Path,
    dataset_hash: str,
    mode: str,
    ablation: str,
    providers_raw: str,
    personas_raw: str,
    unit_id: str | None,
    limit: int | None,
    cost_budget_usd: float | None,
    explicit_tool_use_prompt: bool,
    context_window_tokens: int | None,
) -> None:
    """Run the multi-turn SOC-agent benchmark over a built index.

    Unit selection defaults to stratified sampling, deterministic in
    ``(dataset_hash, sample_seed, mode)``. ``--unit-id ID`` or ``--limit N``
    override the sampler for one-off / debugging runs.
    """
    cfg = load_benchmark_config(config_path)
    schema = load_schema(cfg.schema_path)
    pricing = load_pricing(cfg.pricing_path)
    parts = load_prompts(prompts_dir)

    # Index
    index_dir = cfg.paths.index_root / dataset_hash
    if not index_dir.exists():
        raise click.ClickException(
            f"index dir {index_dir} not found — run `socbench build-index` first."
        )

    # Personas
    if personas_raw.strip() == "all":
        personas_selected = list(cfg.agent.personas.keys())
    else:
        personas_selected = [n.strip() for n in personas_raw.split(",") if n.strip()]
    unknown = [p for p in personas_selected if p not in cfg.agent.personas]
    if unknown:
        raise click.ClickException(
            f"unknown persona(s): {unknown}; known: {sorted(cfg.agent.personas)}"
        )
    personas = {p: cfg.agent.personas[p] for p in personas_selected}

    # Providers (config-driven enabled list + 'all' / CSV)
    cfg_enabled = [name for name, pc in cfg.providers.items() if pc.enabled]
    provider_names = _resolve_providers(providers_raw, cfg_enabled)

    adapters: dict[str, object] = {}
    for name in provider_names:
        if name == "mock":
            model = "mock-default"
        elif name == "open_source":
            # OPEN_SOURCE_MODEL (set by the deploy entrypoint to vLLM's served name)
            # takes precedence over the config default, which varies per deployment.
            env_model = os.environ.get("OPEN_SOURCE_MODEL")
            if env_model:
                model = env_model
            elif name in cfg.providers:
                model = cfg.providers[name].model  # type: ignore[assignment]
            else:
                raise click.ClickException(
                    "open_source provider: add an 'open_source' entry to config or "
                    "set OPEN_SOURCE_MODEL env var"
                )
        else:
            if name not in cfg.providers:
                raise click.ClickException(
                    f"provider {name!r} not in config; "
                    f"known: {sorted(cfg.providers)} + 'mock'"
                )
            model = cfg.providers[name].model  # type: ignore[assignment]
        try:
            adapters[name] = build_adapter(name, model)
        except FatalAdapterError as exc:
            raise click.ClickException(
                f"could not construct {name} adapter ({model}): {exc}"
            ) from exc

    # Tool registry + context
    registry = build_default_registry(
        persona_allowlist=cfg.agent.persona_tool_allowlist()
    )
    tool_ctx = ToolContext(index_dir=index_dir)
    output_contract = registry.get("submit_assessment").args_schema

    # Eval units: stratified sampling by default; --unit-id / --limit
    # override for one-off runs.
    all_units = load_eval_units(index_dir)
    mode_cfg = cfg.sampling.smoke if mode == "smoke" else cfg.sampling.full
    if unit_id is not None or limit is not None:
        units = select_eval_units(all_units, unit_id=unit_id, limit=limit)
    else:
        sample = stratified_sample(
            all_units,
            mode=mode,  # type: ignore[arg-type]
            sample_seed=cfg.sampling.sample_seed,
            dataset_hash=dataset_hash,
            mode_cfg=mode_cfg,
        )
        units = sample.selected
        log.info(
            "stratified_sample",
            extra={
                "total_selected": sample.report.total_selected,
                "per_stratum_selected": sample.report.per_stratum_selected,
                "undersampled_strata": sample.report.undersampled_strata,
                "capped": sample.report.capped,
            },
        )
    if not units:
        raise click.ClickException(
            "no eval units selected — the index may be empty or sampling found no strata."
        )

    # Build the run
    run_id = generate_run_id(
        ablation=ablation,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        providers=list(adapters),
        dataset_hash=dataset_hash,
    )

    budget = cost_budget_usd if cost_budget_usd is not None else mode_cfg.cost_budget_usd

    rc = RunConfig(
        run_id=run_id,
        runs_root=cfg.paths.runs_root,
        dataset_hash=dataset_hash,
        index_dir=index_dir,
        mode=mode,  # type: ignore[arg-type]
        ablation=ablation,  # type: ignore[arg-type]
        sample_seed=cfg.sampling.sample_seed,
        cost_budget_usd=budget,
        cost_usd_cap_per_rendering=cfg.agent.cost_usd_cap_per_rendering,
        min_investigation_tool_calls=cfg.agent.min_investigation_tool_calls,
        explicit_tool_use_scaffold=explicit_tool_use_prompt,
        context_token_budget=context_window_tokens,
    )
    provider_temperatures = {
        name: cfg.providers[name].temperature
        for name in adapters
        if name in cfg.providers
    }
    provider_concurrency = {
        name: cfg.providers[name].max_concurrency
        for name in adapters
        if name in cfg.providers
    }
    provider_max_output_tokens = {
        name: cfg.providers[name].max_output_tokens
        for name in adapters
        if name in cfg.providers
    }
    provider_reasoning_budgets = {
        name: cfg.providers[name].reasoning_budget_tokens
        for name in adapters
        if name in cfg.providers
        and cfg.providers[name].reasoning_budget_tokens is not None
    }
    provider_budget_multipliers = {
        name: cfg.providers[name].effective_budget_multiplier
        for name in adapters
        if name in cfg.providers
    }
    provider_wall_clock_overrides = {
        name: cfg.providers[name].wall_clock_override_seconds
        for name in adapters
        if name in cfg.providers
        and cfg.providers[name].wall_clock_override_seconds is not None
    }
    provider_circuit_threshold = {
        name: cfg.providers[name].circuit_breaker_threshold
        for name in adapters
        if name in cfg.providers
    }
    runner = Runner(
        run_config=rc,
        adapters=adapters,  # type: ignore[arg-type]
        personas=personas,
        tool_registry=registry,
        tool_context=tool_ctx,
        prompt_parts=parts,
        label_inference=schema.label_inference,
        pricing=pricing,
        output_contract_schema=output_contract,
        provider_temperatures=provider_temperatures,
        provider_concurrency=provider_concurrency,
        provider_max_output_tokens=provider_max_output_tokens,
        provider_reasoning_budgets=provider_reasoning_budgets,
        provider_budget_multipliers=provider_budget_multipliers,
        provider_wall_clock_overrides=provider_wall_clock_overrides,
        provider_circuit_threshold=provider_circuit_threshold,
    )
    log.info(
        "run_start",
        extra={
            "run_id": run_id,
            "providers": list(adapters),
            "personas": list(personas),
            "ablation": ablation,
            "mode": mode,
            "units": len(units),
            "provider_concurrency": provider_concurrency,
        },
    )
    outcome = runner.run_sync(units)
    click.echo(
        json.dumps(
            {
                "run_id": run_id,
                "rendering_count": outcome.rendering_count,
                "total_cost_usd": outcome.total_cost_usd,
                "elapsed_wall_ms": outcome.elapsed_wall_ms,
                "aggregate_rendering_wall_ms": outcome.aggregate_rendering_wall_ms,
                "aborted_for_budget": outcome.aborted_for_budget,
                "run_dir": str(outcome.paths.root),
            },
            indent=2,
            sort_keys=True,
        )
    )


@cli.command("aggregate")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config/benchmark_config.yaml"),
    show_default=True,
)
@click.option("--dataset-hash", required=True, help="Reproducibility key: index dataset_hash.")
@click.option(
    "--seed",
    type=int,
    default=None,
    help="sample_seed to aggregate. Defaults to sampling.sample_seed from config.",
)
def aggregate_cmd(config_path: Path, dataset_hash: str, seed: int | None) -> None:
    """Join ablation runs sharing (dataset_hash, seed) into an ablation summary."""
    cfg = load_benchmark_config(config_path)
    sample_seed = seed if seed is not None else cfg.sampling.sample_seed
    try:
        out_path = aggregate_ablations(
            runs_root=cfg.paths.runs_root,
            ablations_root=cfg.paths.ablations_root,
            dataset_hash=dataset_hash,
            sample_seed=sample_seed,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    summary = json.loads(out_path.read_text(encoding="utf-8"))
    click.echo(
        json.dumps(
            {
                "ablation_summary": str(out_path),
                "runs": summary.get("runs", {}),
                "missing_ablations": summary.get("missing_ablations", []),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    cli()

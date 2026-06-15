"""Step 5 — agent loop, cost rollup, run orchestrator, artifact writers.

Three layers, one module:

1. :class:`AgentLoop` runs ONE rendering — one (eval_unit, persona, provider)
   triple — multi-turn until ``submit_assessment`` or a cap is hit. Enforces
   per-persona budgets with force-final-answer, the persona tool allowlist,
   and the split between recoverable tool-call schema violations and strict
   final-answer validation.

2. :class:`Runner` orchestrates many renderings and writes the on-disk
   artifacts under ``runs/<run_id>/``. Each artifact has a single
   tightly-typed writer; there's no schema drift between models.py
   and the JSONL rows.

3. Helpers — ``load_eval_units``, ``select_eval_units``,
   ``build_user_kickoff_message``, and ``compute_summary`` — are small,
   pure, and test-friendly.

The module deliberately knows nothing about specific providers. It receives
adapters via the :class:`socbench.providers.Adapter` interface.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl
from pydantic import ValidationError

from socbench.config import PersonaPolicy, PricingTable
from socbench.hashing import hash_obj
from socbench.logging_config import get_logger
from socbench.models import (
    AblationTag,
    EvalUnit,
    EvalUnitSummary,
    PredictionRow,
    RenderingResult,
    RunArtifactsPaths,
    RunMetadata,
    RunMode,
    SubmitAssessment,
    ToolCall,
    ToolResult,
)
from socbench.prompts import (
    Ablation,
    PromptParts,
    compose,
    playbooks_manifest_sha,
    prompts_manifest_sha,
)
from socbench.providers import (
    Adapter,
    AdapterRequest,
    AdapterResponse,
    AdapterToolCall,
    FatalAdapterError,
    Message,
    RetryableAdapterError,
)
from socbench.schema import LabelInference
from socbench.scoring import GoldIndex, load_gold, score_unit
from socbench.tools import ToolContext, ToolRegistry
from socbench.tools.base import ToolSchemaViolation

log = get_logger(__name__)


# Per-tool-result size cap. Real tools already cap rows via `limit`; this is a
# defense against accidental large payloads (e.g. a future tool that doesn't
# honor `max_results`). 64 KiB is generous for a single tool turn.
_TOOL_RESULT_MAX_BYTES = 64 * 1024


# Per-rendering one-time message injected when any cap fires.
_FORCE_FINAL_MESSAGE = (
    "Budget reached. Submit your best assessment now by calling "
    "`submit_assessment`. No further tool calls will be processed."
)


# ---------------------------------------------------------------------------
# Eval unit selection
# ---------------------------------------------------------------------------


def _tool_descriptor(tool: Any) -> dict[str, Any]:
    """Compact (name, description, schema) tuple exposed to adapters & compose."""
    return {"name": tool.name, "description": tool.description, "schema": tool.args_schema}


def load_eval_units(index_dir: Path) -> list[EvalUnit]:
    """Read ``eval_units.jsonl`` from a built index."""
    path = index_dir / "eval_units.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"eval_units.jsonl not found at {path}")
    units = [
        EvalUnit.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    units.sort(key=lambda u: u.eval_unit_id)
    return units


def select_eval_units(
    all_units: list[EvalUnit],
    *,
    unit_id: str | None = None,
    limit: int | None = None,
) -> list[EvalUnit]:
    """Explicit override selector for a run's eval units.

    Used when a caller passes ``unit_id`` (exact match) or ``limit`` (first N
    by sorted eval_unit_id) instead of the default stratified sampler. Passing
    neither is an explicit error so we don't silently run on something the
    caller didn't intend.
    """
    if unit_id is not None and limit is not None:
        raise ValueError("specify --unit-id or --limit, not both")
    if unit_id is not None:
        matches = [u for u in all_units if u.eval_unit_id == unit_id]
        if not matches:
            raise KeyError(
                f"no eval_unit with id={unit_id!r}; "
                f"index has {len(all_units)} units"
            )
        return matches
    if limit is not None:
        if limit <= 0:
            raise ValueError("--limit must be a positive integer")
        return all_units[:limit]
    raise ValueError(
        "must pass --unit-id or --limit"
    )


def build_user_kickoff_message(unit: EvalUnit) -> str:
    """The first user-role message of every rendering.

    Carries the eval unit's *non-label* metadata only — the model uses tools
    to discover flow_ids within scope. Never includes ``gold_label``,
    ``malicious_flow_count``, or anything else label-derived.
    """
    lines = [
        f"Eval unit: {unit.eval_unit_id}",
        f"Type: {unit.unit_type}",
        f"Source IP: {unit.src_ip}",
    ]
    if unit.unit_type == "pair_timeline" and unit.dst_ip:
        lines.append(f"Destination IP: {unit.dst_ip}")
    else:
        lines.append(f"Distinct destinations: {unit.distinct_destinations}")
    lines += [
        f"Flow count: {unit.flow_count}",
        f"Time window (epoch seconds): {unit.ts_start_min:.3f} .. {unit.ts_start_max:.3f}",
        "",
        (
            "Use the available tools to investigate this unit, then call "
            "`submit_assessment` with your verdict and the flow_ids you "
            "judged malicious. Every flow_id you cite must appear in a tool "
            "response from this conversation."
        ),
    ]
    if unit.unit_type == "host_egress":
        lines.append(
            "This is a fan-out unit with many flows across destinations. "
            "Rather than enumerating every malicious flow_id, you may name the "
            "malicious destination IPs in `malicious_destinations`; the harness "
            "expands each to all of its flows in scope."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent loop (one rendering)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentLoopConfig:
    """Inputs to a single rendering."""

    persona: str
    persona_policy: PersonaPolicy
    ablation: AblationTag
    cost_usd_cap_per_rendering: float
    max_retries: int = 3
    max_output_tokens: int = 2048
    tool_max_results_cap: int = 200
    temperature: float | None = None  # None → omit from the provider call


@dataclass
class AgentLoopResult:
    """Everything one rendering produces."""

    rendering_result: RenderingResult
    tool_calls: list[ToolCall]
    tool_results: list[ToolResult]
    predictions: list[PredictionRow]
    submit_args: dict[str, Any] | None  # validated SubmitAssessment dict; None if invalid


class AgentLoop:
    """One rendering. Stateful only for the duration of :meth:`run`."""

    def __init__(
        self,
        *,
        config: AgentLoopConfig,
        adapter: Adapter,
        tool_registry: ToolRegistry,
        tool_context: ToolContext,
        prompt_parts: PromptParts,
        label_inference: LabelInference,
        pricing: PricingTable,
        output_contract_schema: dict[str, Any],
    ) -> None:
        self.cfg = config
        self.adapter = adapter
        self.tool_registry = tool_registry
        self.tool_context = tool_context
        self.prompt_parts = prompt_parts
        self.label_inference = label_inference
        self.pricing = pricing
        self.output_contract_schema = output_contract_schema

    # -- public --------------------------------------------------------------

    def run_sync(self, unit: EvalUnit) -> AgentLoopResult:
        """Blocking wrapper around :meth:`run` for synchronous callers/tests."""
        return asyncio.run(self.run(unit))

    async def run(self, unit: EvalUnit) -> AgentLoopResult:
        """Run the multi-turn loop on one eval unit, return artifacts."""
        self.adapter.reset()
        rendering_id = (
            f"{unit.eval_unit_id}__{self.adapter.provider_name}__{self.cfg.persona}"
        )
        ablation: Ablation = self._compose_ablation()
        system_prompt = self._compose_system_prompt(ablation)
        messages: list[Message] = [
            Message(role="user", content=build_user_kickoff_message(unit))
        ]
        tool_schemas = self._persona_tool_schemas(ablation)

        # State
        turns_used = 0
        tool_calls_used = 0
        cost_accum = 0.0
        rendering_start = time.monotonic()
        predictions: list[PredictionRow] = []
        tool_calls: list[ToolCall] = []
        tool_results: list[ToolResult] = []
        submit_args: dict[str, Any] | None = None
        final_valid = False
        forced_final_answer = False
        cap_hit = False
        cap_hit_reason: str | None = None
        adapter_fatal = False

        while True:
            # Cap check at TOP of each turn (the next call would push us over).
            wall_ms = int((time.monotonic() - rendering_start) * 1000)
            cap_reason = self._check_cap(turns_used, tool_calls_used, wall_ms, cost_accum)
            force = cap_reason is not None

            if force:
                cap_hit = True
                cap_hit_reason = cap_reason
                forced_final_answer = True
                # Append the budget-reached message so the model sees why.
                messages.append(Message(role="user", content=_FORCE_FINAL_MESSAGE))

            request = AdapterRequest(
                system_prompt=system_prompt,
                messages=list(messages),
                tool_schemas=list(tool_schemas),
                output_contract_schema=self.output_contract_schema,
                output_contract_tool_name="submit_assessment",
                max_output_tokens=self.cfg.max_output_tokens,
                temperature=self.cfg.temperature,
                force_final_answer=force,
            )

            try:
                response = await self._invoke_with_retry(request)
            except FatalAdapterError as exc:
                log.error(
                    "adapter_fatal",
                    extra={"rendering_id": rendering_id, "error": str(exc)},
                )
                # Mark invalid and stop the rendering; nothing to recover.
                adapter_fatal = True
                cost_call = 0.0
                predictions.append(
                    self._prediction_row(
                        unit=unit,
                        rendering_id=rendering_id,
                        turn_index=turns_used,
                        tool_name=None,
                        tool_args_hash=None,
                        tool_result_truncated=False,
                        response=None,
                        cost_call=cost_call,
                        elapsed_ms=0,
                    )
                )
                break

            cost_call = self._cost_of(response)
            cost_accum += cost_call
            tool_name = (
                response.tool_call.name if response.tool_call else (
                    "submit_assessment" if response.submit_assessment_args else None
                )
            )
            tool_args_hash = (
                hash_obj(response.tool_call.args) if response.tool_call else None
            )
            predictions.append(
                self._prediction_row(
                    unit=unit,
                    rendering_id=rendering_id,
                    turn_index=turns_used,
                    tool_name=tool_name,
                    tool_args_hash=tool_args_hash,
                    tool_result_truncated=False,
                    response=response,
                    cost_call=cost_call,
                    elapsed_ms=response.wall_time_ms,
                )
            )
            turns_used += 1

            # --- terminal: submit_assessment ---
            if response.submit_assessment_args is not None:
                submit_args, final_valid = self._validate_submit(
                    response.submit_assessment_args
                )
                break

            # --- terminal: forced but model returned no submission ---
            if force and response.tool_call is not None:
                # The model defied the force-final instruction; we honor the
                # budget cap and mark invalid. Reflected as final_valid=False.
                log.warning(
                    "force_final_ignored_by_model",
                    extra={
                        "rendering_id": rendering_id,
                        "tool_call": response.tool_call.name,
                    },
                )
                break

            # --- tool turn: dispatch ---
            if response.tool_call is None:
                # Neither tool_call nor submit_assessment. Treat as
                # informational text (rare with structured-output providers);
                # let the loop continue and rely on caps to terminate.
                messages.append(Message(role="assistant", content=response.text))
                continue

            call = response.tool_call
            messages.append(
                Message(role="assistant", content=response.text, tool_call=call)
            )
            tool_calls_used += 1
            tc = ToolCall(
                rendering_id=rendering_id,
                turn_index=turns_used - 1,
                tool_name=call.name,
                args=dict(call.args),
                args_hash=hash_obj(call.args),
            )
            tool_calls.append(tc)
            tr = await self._dispatch_tool(rendering_id, turns_used - 1, call)
            tool_results.append(tr)
            messages.append(
                Message(
                    role="tool",
                    content=json.dumps(
                        tr.payload if tr.ok else {"error": tr.error},
                        sort_keys=True,
                    ),
                    tool_use_id=call.id,
                )
            )

        # Build the RenderingResult once the loop has terminated.
        wall_ms_final = int((time.monotonic() - rendering_start) * 1000)
        rendering_result = RenderingResult(
            rendering_id=rendering_id,
            eval_unit_id=unit.eval_unit_id,
            provider=self.adapter.provider_name,
            persona=self.cfg.persona,
            turns_used=turns_used,
            tool_calls_used=tool_calls_used,
            wall_time_ms=wall_ms_final,
            cost_usd=round(cost_accum, 6),
            cap_hit=cap_hit,
            cap_hit_reason=cap_hit_reason,  # type: ignore[arg-type]
            final_valid=final_valid and not forced_final_answer,
            forced_final_answer=forced_final_answer,
            adapter_fatal=adapter_fatal,
        )
        return AgentLoopResult(
            rendering_result=rendering_result,
            tool_calls=tool_calls,
            tool_results=tool_results,
            predictions=predictions,
            submit_args=submit_args if final_valid else None,
        )

    # -- private -------------------------------------------------------------

    def _compose_ablation(self) -> Ablation:
        """Coerce the run-level ablation tag into the compose-known set.

        ``single_shot_baseline`` runs are the external single-shot baseline,
        not driven by this agent loop; the CLI rejects it. The fallback keeps
        the runtime defensive.
        """
        if self.cfg.ablation in {"main", "tools_off", "playbooks_off"}:
            return self.cfg.ablation  # type: ignore[return-value]
        return "main"

    def _compose_system_prompt(self, ablation: Ablation) -> str:
        return compose(
            self.prompt_parts,
            persona=self.cfg.persona,
            ablation=ablation,
            output_contract_schema=self.output_contract_schema,
            tool_schemas=self._persona_tool_schemas(ablation),
            label_inference=self.label_inference,
        )

    def _persona_tool_schemas(self, ablation: Ablation) -> list[dict[str, Any]]:
        """Tool schemas visible to the model for this persona × ablation.

        Under ``tools_off`` the allowlist collapses to ``submit_assessment``
        only. ``submit_assessment`` is always available.
        """
        if ablation == "tools_off":
            tool = self.tool_registry.get("submit_assessment")
            return [_tool_descriptor(tool)]
        tools = self.tool_registry.tools_for_persona(self.cfg.persona)
        return [_tool_descriptor(t) for t in tools]

    async def _invoke_with_retry(self, request: AdapterRequest) -> AdapterResponse:
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                return await self.adapter.invoke(request)
            except RetryableAdapterError as exc:
                last = exc
                if attempt == self.cfg.max_retries:
                    raise FatalAdapterError(
                        f"max retries ({self.cfg.max_retries}) exhausted: {exc}"
                    ) from exc
                backoff_s = min(2 ** attempt, 8)
                # Honor the provider's Retry-After hint when present, capped so
                # a single throttled call can't pin a slot for minutes. The
                # await yields the event loop so other renderings proceed.
                retry_after = getattr(exc, "retry_after_seconds", None)
                if retry_after is not None:
                    backoff_s = min(max(backoff_s, float(retry_after)), 30.0)
                await asyncio.sleep(backoff_s)
        # Unreachable; the loop either returns or raises.
        raise FatalAdapterError(f"retry loop fell through: {last}")

    def _check_cap(
        self,
        turns_used: int,
        tool_calls_used: int,
        wall_ms: int,
        cost_accum: float,
    ) -> str | None:
        if turns_used >= self.cfg.persona_policy.max_turns:
            return "turns"
        if tool_calls_used >= self.cfg.persona_policy.max_tool_calls:
            return "tool_calls"
        if wall_ms >= self.cfg.persona_policy.wall_clock_seconds * 1000:
            return "wall_clock"
        if cost_accum >= self.cfg.cost_usd_cap_per_rendering:
            return "cost"
        return None

    async def _dispatch_tool(
        self, rendering_id: str, turn_index: int, call: AdapterToolCall
    ) -> ToolResult:
        # Allowlist check (cheap; the persona × tool matrix is config-driven).
        if not self.tool_registry.is_allowed(self.cfg.persona, call.name):
            return ToolResult(
                rendering_id=rendering_id,
                turn_index=turn_index,
                tool_name=call.name,
                ok=False,
                error=f"tool {call.name!r} not in allowlist for persona {self.cfg.persona!r}",
            )

        # submit_assessment as a regular tool call is a model bug — it must
        # go through the structured-output path. Reject loudly so the model
        # learns from the error rather than blocking the rendering.
        if call.name == "submit_assessment":
            return ToolResult(
                rendering_id=rendering_id,
                turn_index=turn_index,
                tool_name=call.name,
                ok=False,
                error="submit_assessment must be the final structured answer, not a tool call",
            )

        tool = self.tool_registry.get(call.name)
        start = time.monotonic()
        try:
            # Tools are sync + I/O-bound (DuckDB opens a fresh connection per
            # call, so they're thread-safe). Offload to a worker thread so the
            # event loop stays free for other renderings' API calls.
            payload = await asyncio.to_thread(tool, dict(call.args), self.tool_context)
            elapsed = int((time.monotonic() - start) * 1000)
        except ToolSchemaViolation as exc:
            return ToolResult(
                rendering_id=rendering_id,
                turn_index=turn_index,
                tool_name=call.name,
                ok=False,
                error=f"schema_violation: {exc}",
                wall_time_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:  # tool-internal bug; surface as recoverable error
            log.exception("tool_internal_error", extra={"tool": call.name})
            return ToolResult(
                rendering_id=rendering_id,
                turn_index=turn_index,
                tool_name=call.name,
                ok=False,
                error=f"tool_internal_error: {type(exc).__name__}: {exc}",
                wall_time_ms=int((time.monotonic() - start) * 1000),
            )

        truncated = False
        serialised = json.dumps(payload, sort_keys=True)
        if len(serialised) > _TOOL_RESULT_MAX_BYTES:
            truncated = True
            log.warning(
                "tool_result_truncated",
                extra={"tool": call.name, "bytes": len(serialised)},
            )

        return ToolResult(
            rendering_id=rendering_id,
            turn_index=turn_index,
            tool_name=call.name,
            ok=True,
            payload=payload,
            truncated=truncated,
            wall_time_ms=elapsed,
        )

    def _validate_submit(
        self, args: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        """Strict final-answer validation. No repair."""
        try:
            validated = SubmitAssessment.model_validate(args)
        except ValidationError as exc:
            log.warning("submit_assessment_invalid", extra={"errors": exc.errors()})
            return None, False
        return validated.model_dump(), True

    def _cost_of(self, response: AdapterResponse) -> float:
        u = response.usage
        try:
            return self.pricing.cost_usd(
                provider=self.adapter.provider_name,
                model=self.adapter.model,
                prompt_tokens=u.prompt_tokens,
                cached_tokens=u.cached_tokens,
                output_tokens=u.output_tokens,
                reasoning_tokens=u.reasoning_tokens,
            )
        except KeyError:
            # Unknown model in pricing.yaml → zero cost rather than crash a
            # debugging run. The missing entry is already logged by callers.
            return 0.0

    def _prediction_row(
        self,
        *,
        unit: EvalUnit,
        rendering_id: str,
        turn_index: int,
        tool_name: str | None,
        tool_args_hash: str | None,
        tool_result_truncated: bool,
        response: AdapterResponse | None,
        cost_call: float,
        elapsed_ms: int,
    ) -> PredictionRow:
        u = response.usage if response is not None else None
        return PredictionRow(
            eval_unit_id=unit.eval_unit_id,
            rendering_id=rendering_id,
            provider=self.adapter.provider_name,
            persona=self.cfg.persona,
            turn_index=turn_index,
            tool_name=tool_name,
            tool_call_args_hash=tool_args_hash,
            tool_result_truncated=tool_result_truncated,
            prompt_tokens=u.prompt_tokens if u else 0,
            cached_tokens=u.cached_tokens if u else 0,
            output_tokens=u.output_tokens if u else 0,
            reasoning_tokens=u.reasoning_tokens if u else 0,
            wall_time_ms=elapsed_ms,
            cost_usd=round(cost_call, 6),
        )


# ---------------------------------------------------------------------------
# Runner (orchestrates many renderings, writes artifacts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """Inputs that scope a whole run (one ablation, one mode)."""

    run_id: str
    runs_root: Path
    dataset_hash: str
    index_dir: Path
    mode: RunMode
    ablation: AblationTag
    sample_seed: int
    cost_budget_usd: float
    cost_usd_cap_per_rendering: float


@dataclass
class RunOutcome:
    """What :meth:`Runner.run` returns to the CLI."""

    paths: RunArtifactsPaths
    rendering_count: int
    total_cost_usd: float
    # True end-to-end run duration (concurrency-aware).
    elapsed_wall_ms: int
    # Sum of per-rendering wall times (sequential-equivalent compute).
    aggregate_rendering_wall_ms: int
    aborted_for_budget: bool
    summary: dict[str, Any]


def prepare_run_artifacts(runs_root: Path, run_id: str) -> RunArtifactsPaths:
    """Create the on-disk skeleton for a run and return its paths."""
    root = runs_root / run_id
    root.mkdir(parents=True, exist_ok=True)
    prompts_used = root / "prompts_used"
    prompts_used.mkdir(exist_ok=True)
    paths = RunArtifactsPaths(
        root=root,
        run_metadata=root / "run_metadata.json",
        predictions_raw_jsonl=root / "predictions_raw.jsonl",
        predictions_raw_parquet=root / "predictions_raw.parquet",
        predictions_per_flow_parquet=root / "predictions_per_flow.parquet",
        renderings_jsonl=root / "renderings.jsonl",
        eval_units_summary_jsonl=root / "eval_units_summary.jsonl",
        tool_calls_jsonl=root / "tool_calls.jsonl",
        summary_json=root / "summary.json",
        prompts_used_dir=prompts_used,
        index_manifest_link=root / "index_manifest_link.json",
    )
    return paths


def _append_jsonl(path: Path, row_json: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(row_json)
        f.write("\n")


class Runner:
    """Orchestrates many renderings and writes the per-run artifact tree."""

    def __init__(
        self,
        *,
        run_config: RunConfig,
        adapters: dict[str, Adapter],
        personas: dict[str, PersonaPolicy],
        tool_registry: ToolRegistry,
        tool_context: ToolContext,
        prompt_parts: PromptParts,
        label_inference: LabelInference,
        pricing: PricingTable,
        output_contract_schema: dict[str, Any],
        provider_temperatures: dict[str, float | None] | None = None,
        provider_concurrency: dict[str, int] | None = None,
        provider_max_output_tokens: dict[str, int] | None = None,
        provider_budget_multipliers: dict[str, float] | None = None,
        provider_circuit_threshold: dict[str, int] | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("Runner requires at least one adapter")
        if not personas:
            raise ValueError("Runner requires at least one persona")
        self.cfg = run_config
        self.adapters = adapters
        self.personas = personas
        self.tool_registry = tool_registry
        self.tool_context = tool_context
        self.prompt_parts = prompt_parts
        self.label_inference = label_inference
        self.pricing = pricing
        self.output_contract_schema = output_contract_schema
        # Per-provider sampling temperature; missing/None → omit from the call.
        self.provider_temperatures = provider_temperatures or {}
        # Per-provider max in-flight renderings. Missing → 1 (sequential),
        # which keeps stateful adapters (e.g. the mock) correct by default.
        self.provider_concurrency = provider_concurrency or {}
        self.provider_max_output_tokens = provider_max_output_tokens or {}
        # Per-provider loop-budget scaling (max_turns / max_tool_calls /
        # wall_clock_seconds / cost cap). Missing → 1.0 (use persona policy as-is).
        self.provider_budget_multipliers = provider_budget_multipliers or {}
        # Per-provider circuit breaker: after this many *consecutive* fatal
        # renderings, stop submitting new work for that provider (a hard-down
        # or quota-exhausted provider shouldn't burn the whole wall-clock).
        # 0/missing → disabled.
        self.provider_circuit_threshold = provider_circuit_threshold or {}

    def run_sync(self, units: list[EvalUnit]) -> RunOutcome:
        """Blocking wrapper around :meth:`run` for the CLI and tests."""
        return asyncio.run(self.run(units))

    async def run(self, units: list[EvalUnit]) -> RunOutcome:
        paths = prepare_run_artifacts(self.cfg.runs_root, self.cfg.run_id)
        started_at = datetime.now(UTC).isoformat()

        # Gold is read once per run; the scorer is the only component
        # besides the index builder allowed to see ground truth.
        gold: GoldIndex = load_gold(self.cfg.index_dir)

        # Write index_manifest_link once at the start; idempotent so re-runs
        # under the same run_id update in place.
        paths.index_manifest_link.write_text(
            json.dumps(
                {
                    "dataset_hash": self.cfg.dataset_hash,
                    "index_dir": str(self.cfg.index_dir.resolve()),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        # Truncate per-run JSONLs so reruns are clean.
        for p in (
            paths.predictions_raw_jsonl,
            paths.renderings_jsonl,
            paths.eval_units_summary_jsonl,
            paths.tool_calls_jsonl,
        ):
            p.write_text("", encoding="utf-8")

        # Snapshot composed prompts once per (persona × provider) — they're
        # stable across renderings within a run because the system prompt is
        # the cacheable prefix.
        ablation: Ablation = (
            self.cfg.ablation  # type: ignore[assignment]
            if self.cfg.ablation in {"main", "tools_off", "playbooks_off"}
            else "main"
        )
        for persona in self.personas:
            for provider_name in self.adapters:
                snapshot = compose(
                    self.prompt_parts,
                    persona=persona,
                    ablation=ablation,
                    output_contract_schema=self.output_contract_schema,
                    tool_schemas=self._snapshot_tool_schemas(persona, ablation),
                    label_inference=self.label_inference,
                )
                snap_path = paths.prompts_used_dir / f"{persona}_{provider_name}.txt"
                snap_path.write_text(snapshot, encoding="utf-8")

        # Expanded per-flow predictions for every flow in every evaluated unit.
        # Written once at the end as predictions_per_flow.parquet.
        summaries: list[EvalUnitSummary] = []
        per_flow_rows: list[dict[str, Any]] = []

        # Every rendering is one coroutine on a single event loop. Renderings
        # are independent and I/O-bound (each waits on an LLM API call), and a
        # per-provider asyncio.Semaphore bounds how many of that provider's
        # calls are in flight at once — so a throttled provider can't starve
        # the others and we never exceed its rate limit. Tasks parked on a
        # full semaphore haven't started network work yet, so cancelling them
        # (budget abort / open breaker) is clean. All artifact writes +
        # accumulation happen on THIS coroutine as tasks complete, so writes
        # stay single-writer and lock-free and the incremental JSONL
        # checkpoints remain intact.
        semaphores: dict[str, asyncio.Semaphore] = {
            provider_name: asyncio.Semaphore(
                max(1, self.provider_concurrency.get(provider_name, 1))
            )
            for provider_name in self.adapters
        }

        async def _bounded(
            unit: EvalUnit,
            persona: str,
            persona_policy: PersonaPolicy,
            provider_name: str,
            adapter: Adapter,
        ) -> tuple[EvalUnit, str, str, AgentLoopResult]:
            async with semaphores[provider_name]:
                result = await self._run_rendering(
                    unit=unit,
                    persona=persona,
                    persona_policy=persona_policy,
                    provider_name=provider_name,
                    adapter=adapter,
                )
            return unit, persona, provider_name, result

        tasks: list[asyncio.Task[tuple[EvalUnit, str, str, AgentLoopResult]]] = []
        task_provider: dict[asyncio.Task[Any], str] = {}
        for unit in units:
            for persona, persona_policy in self.personas.items():
                for provider_name, adapter in self.adapters.items():
                    task = asyncio.ensure_future(
                        _bounded(
                            unit, persona, persona_policy, provider_name, adapter
                        )
                    )
                    tasks.append(task)
                    task_provider[task] = provider_name

        # Two wall-time views: `wall_start` measures TRUE elapsed run time
        # (renderings run concurrently), while `total_wall_ms` sums each
        # rendering's own wall time (sequential-equivalent compute). Under
        # concurrency the latter is much larger than the former.
        wall_start = time.monotonic()
        total_cost = 0.0
        total_wall_ms = 0
        rendering_count = 0
        aborted = False
        # Per-provider consecutive-fatal counters + a set of providers whose
        # breaker has tripped. Mutated only on this (orchestrating) coroutine.
        consecutive_fatal: dict[str, int] = {}
        tripped: set[str] = set()
        try:
            for coro in asyncio.as_completed(tasks):
                try:
                    unit, persona, provider_name, result = await coro
                except asyncio.CancelledError:
                    continue  # cancelled after a soft budget abort / open breaker
                rendering_count += 1
                cost, wall_ms = self._record_rendering(
                    unit=unit,
                    persona=persona,
                    provider_name=provider_name,
                    result=result,
                    gold=gold,
                    paths=paths,
                    summaries=summaries,
                    per_flow_rows=per_flow_rows,
                )
                total_cost += cost
                total_wall_ms += wall_ms

                # Per-provider circuit breaker on consecutive fatal renderings.
                threshold = self.provider_circuit_threshold.get(provider_name, 0)
                if result.rendering_result.adapter_fatal:
                    consecutive_fatal[provider_name] = (
                        consecutive_fatal.get(provider_name, 0) + 1
                    )
                    if (
                        threshold
                        and provider_name not in tripped
                        and consecutive_fatal[provider_name] >= threshold
                    ):
                        tripped.add(provider_name)
                        log.error(
                            "provider_circuit_open",
                            extra={
                                "provider": provider_name,
                                "consecutive_fatal": consecutive_fatal[provider_name],
                                "threshold": threshold,
                            },
                        )
                        for pending, pend_provider in task_provider.items():
                            if pend_provider == provider_name and not pending.done():
                                pending.cancel()
                else:
                    consecutive_fatal[provider_name] = 0

                if not aborted and total_cost >= self.cfg.cost_budget_usd:
                    log.warning(
                        "cost_budget_exhausted",
                        extra={
                            "total_cost_usd": total_cost,
                            "cost_budget_usd": self.cfg.cost_budget_usd,
                        },
                    )
                    aborted = True
                    # Soft abort: cancel renderings that haven't started yet.
                    # In-flight renderings are already paid for and finish.
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
        finally:
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            # Drain so cancellations settle and no task is left pending.
            await asyncio.gather(*tasks, return_exceptions=True)

        # Per-flow predictions table (one row per flow in every evaluated
        # unit). Written even on budget abort so partial runs keep their rows.
        if per_flow_rows:
            pl.DataFrame(per_flow_rows).write_parquet(
                paths.predictions_per_flow_parquet, compression="zstd", statistics=True
            )

        # Parquet mirror of predictions_raw.jsonl for analysis tooling. Read
        # back from the JSONL so the two artifacts are guaranteed identical.
        if (
            paths.predictions_raw_jsonl.exists()
            and paths.predictions_raw_jsonl.stat().st_size > 0
        ):
            pl.read_ndjson(paths.predictions_raw_jsonl).write_parquet(
                paths.predictions_raw_parquet, compression="zstd", statistics=True
            )

        # Build per-run summary with latency + scoring + cache rollups.
        provider_models = {name: ad.model for name, ad in self.adapters.items()}
        summary = compute_summary(
            summaries,
            paths.predictions_raw_jsonl,
            pricing=self.pricing,
            provider_models=provider_models,
        )
        elapsed_wall_ms = int((time.monotonic() - wall_start) * 1000)
        summary["aborted_for_budget"] = aborted
        summary["rendering_count"] = rendering_count
        summary["total_cost_usd"] = round(total_cost, 6)
        # True end-to-end run duration (concurrency-aware).
        summary["elapsed_wall_ms"] = elapsed_wall_ms
        # Sum of per-rendering wall times — sequential-equivalent compute, NOT
        # elapsed time. Renamed from the old misleading `total_wall_time_ms`.
        summary["aggregate_rendering_wall_ms"] = total_wall_ms
        paths.summary_json.write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
        )

        # Run metadata last so finished_at_utc reflects actual completion.
        meta = RunMetadata(
            run_id=self.cfg.run_id,
            spec_id="socbench-agent",
            mode=self.cfg.mode,
            ablation=self.cfg.ablation,
            dataset_hash=self.cfg.dataset_hash,
            sample_seed=self.cfg.sample_seed,
            prompts_manifest_sha=prompts_manifest_sha(self.prompt_parts),
            playbooks_manifest_sha=playbooks_manifest_sha(
                self.prompt_parts, ablation=ablation
            ),
            tools_manifest_sha=self.tool_registry.manifest_sha(),
            provider_models=provider_models,
            pricing_snapshot_date=str(self.pricing.snapshot_date),
            cost_usd_cap_per_rendering=self.cfg.cost_usd_cap_per_rendering,
            cost_budget_usd=self.cfg.cost_budget_usd,
            image_tag="local-dev",
            git_sha=None,
            host=socket.gethostname(),
            started_at_utc=started_at,
            finished_at_utc=datetime.now(UTC).isoformat(),
        )
        paths.run_metadata.write_text(
            meta.model_dump_json(indent=2), encoding="utf-8"
        )

        return RunOutcome(
            paths=paths,
            rendering_count=rendering_count,
            total_cost_usd=round(total_cost, 6),
            elapsed_wall_ms=elapsed_wall_ms,
            aggregate_rendering_wall_ms=total_wall_ms,
            aborted_for_budget=aborted,
            summary=summary,
        )

    async def _run_rendering(
        self,
        *,
        unit: EvalUnit,
        persona: str,
        persona_policy: PersonaPolicy,
        provider_name: str,
        adapter: Adapter,
    ) -> AgentLoopResult:
        """Run one rendering. Builds a fresh AgentLoop and touches only the
        shared (concurrency-safe) adapter, tool registry, and read-only
        context. Awaited as one coroutine per (unit, persona, provider).
        """
        cost_cap = self.cfg.cost_usd_cap_per_rendering
        multiplier = self.provider_budget_multipliers.get(provider_name, 1.0)
        if multiplier != 1.0:
            persona_policy = persona_policy.model_copy(
                update={
                    "max_turns": max(1, round(persona_policy.max_turns * multiplier)),
                    "max_tool_calls": max(
                        1, round(persona_policy.max_tool_calls * multiplier)
                    ),
                    "wall_clock_seconds": max(
                        1, round(persona_policy.wall_clock_seconds * multiplier)
                    ),
                }
            )
            cost_cap *= multiplier
        loop_config_kwargs: dict[str, Any] = dict(
            persona=persona,
            persona_policy=persona_policy,
            ablation=self.cfg.ablation,
            cost_usd_cap_per_rendering=cost_cap,
            temperature=self.provider_temperatures.get(provider_name),
        )
        max_out = self.provider_max_output_tokens.get(provider_name)
        if max_out is not None:
            loop_config_kwargs["max_output_tokens"] = max_out
        loop = AgentLoop(
            config=AgentLoopConfig(**loop_config_kwargs),
            adapter=adapter,
            tool_registry=self.tool_registry,
            tool_context=self.tool_context,
            prompt_parts=self.prompt_parts,
            label_inference=self.label_inference,
            pricing=self.pricing,
            output_contract_schema=self.output_contract_schema,
        )
        return await loop.run(unit)

    def _record_rendering(
        self,
        *,
        unit: EvalUnit,
        persona: str,
        provider_name: str,
        result: AgentLoopResult,
        gold: GoldIndex,
        paths: RunArtifactsPaths,
        summaries: list[EvalUnitSummary],
        per_flow_rows: list[dict[str, Any]],
    ) -> tuple[float, int]:
        """Score one rendering, append its artifacts, and collect its rows.

        Runs only on the orchestrating coroutine (the ``as_completed``
        consumer), so the JSONL appends and the ``summaries`` /
        ``per_flow_rows`` lists need no locking. Returns
        ``(cost_usd, wall_time_ms)`` for the caller to accumulate.
        """
        # Persist artifacts.
        _append_jsonl(
            paths.renderings_jsonl,
            result.rendering_result.model_dump_json(),
        )
        for pr in result.predictions:
            _append_jsonl(paths.predictions_raw_jsonl, pr.model_dump_json())
        for tc in result.tool_calls:
            _append_jsonl(paths.tool_calls_jsonl, tc.model_dump_json())

        # Build the per-unit summary with the three scoring lenses.
        # Predictions are clamped to the unit's seeded flow set inside
        # `score_unit`.
        predicted_ids = (
            result.submit_args.get("malicious_flow_indices", [])
            if result.submit_args
            else []
        )
        predicted_dsts = (
            result.submit_args.get("malicious_destinations", [])
            if result.submit_args
            else []
        )
        verdict = result.submit_args.get("verdict") if result.submit_args else None
        confidence = (
            result.submit_args.get("confidence") if result.submit_args else None
        )
        rationale = (
            result.submit_args.get("rationale") if result.submit_args else None
        )
        observed_flow_ids, observed_destinations = _observed_from_tool_results(
            result.tool_results
        )
        unit_score = score_unit(
            unit,
            list(predicted_ids),
            gold,
            verdict=verdict,
            predicted_destinations=list(predicted_dsts),
            observed_flow_ids=observed_flow_ids,
            observed_destinations=observed_destinations,
        )
        summary = EvalUnitSummary(
            eval_unit_id=unit.eval_unit_id,
            unit_type=unit.unit_type,
            gold_label=unit.gold_label,
            provider=provider_name,
            persona=persona,
            cost_usd=result.rendering_result.cost_usd,
            wall_time_ms=result.rendering_result.wall_time_ms,
            renderings_count=1,
            unit_first_pass_valid=(
                result.rendering_result.final_valid
                and not result.rendering_result.forced_final_answer
            ),
            verdict=verdict,
            defect=unit_score.defect,
            confidence=confidence,
            rationale=rationale,
            predicted_malicious_flow_ids=unit_score.predicted_in_scope,
            submitted_malicious_flow_indices=[int(f) for f in predicted_ids],
            submitted_malicious_destinations=[str(d) for d in predicted_dsts],
            **unit_score.metric_fields(),
        )
        summaries.append(summary)
        _append_jsonl(paths.eval_units_summary_jsonl, summary.model_dump_json())

        # Expand to one row per flow in the unit (predicted vs gold).
        pred_set = set(unit_score.predicted_in_scope)
        gold_set = set(unit_score.gold_malicious_in_scope)
        rendering_id = result.rendering_result.rendering_id
        for fid in unit.flow_ids:
            per_flow_rows.append(
                {
                    "rendering_id": rendering_id,
                    "eval_unit_id": unit.eval_unit_id,
                    "unit_type": unit.unit_type,
                    "provider": provider_name,
                    "persona": persona,
                    "flow_id": int(fid),
                    "predicted": 1 if fid in pred_set else 0,
                    "gold": 1 if fid in gold_set else 0,
                }
            )

        return result.rendering_result.cost_usd, result.rendering_result.wall_time_ms

    def _snapshot_tool_schemas(self, persona: str, ablation: Ablation) -> list[dict[str, Any]]:
        if ablation == "tools_off":
            tool = self.tool_registry.get("submit_assessment")
            return [_tool_descriptor(tool)]
        tools = self.tool_registry.tools_for_persona(persona)
        return [_tool_descriptor(t) for t in tools]


# ---------------------------------------------------------------------------
# Summary / latency rollups
# ---------------------------------------------------------------------------


def _percentile(values: list[int], pct: float) -> int:
    """Nearest-rank percentile (1..100). Defined for non-empty lists."""
    if not values:
        return 0
    sorted_vals = sorted(values)
    k = max(1, min(len(sorted_vals), round(pct / 100.0 * len(sorted_vals))))
    return sorted_vals[k - 1]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _latency_stats(vals: list[int]) -> dict[str, int]:
    """count / mean / p50 / p95 / max for one latency series.

    Latencies are wall-clock measured DURING a concurrent run (per-provider
    `max_concurrency` > 1), so they reflect latency under load, not isolated
    single-call latency. Run with concurrency 1 to measure the latter.
    """
    return {
        "count": len(vals),
        "mean": int(_mean([float(v) for v in vals])),
        "p50": _percentile(vals, 50),
        "p95": _percentile(vals, 95),
        "max": max(vals) if vals else 0,
    }


# Eval-unit gold labels that carry malicious activity (the ones where detection
# F1 is meaningful; benign units mostly test for false positives).
_MALICIOUS_GOLD = {"malicious", "mixed"}


def _observed_from_tool_results(
    tool_results: list[ToolResult],
) -> tuple[set[int], set[str]]:
    """Collect the flow_ids and dst_ips the model actually saw this rendering.

    Walks every successful tool response and harvests ``flow_id`` (int) and
    ``dst_ip`` (str) values wherever they appear. These define what the model
    legitimately observed; scoring clamps citations to them so guessed ids
    earn no credit. Flow_ids reach the model only via ``get_flows`` /
    ``get_pair_timeline`` results, and ``dst_ip``s via any tool that surfaces
    them (``list_pairs``, ``top_destinations``, ``get_flows``), so this
    faithfully reconstructs the observable surface. Key-based, so it is robust
    to which tool produced the value.
    """
    flow_ids: set[int] = set()
    dsts: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "flow_id" and isinstance(v, int) and not isinstance(v, bool):
                    flow_ids.add(v)
                elif k == "dst_ip" and isinstance(v, str):
                    dsts.add(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for tr in tool_results:
        if tr.ok and tr.payload is not None:
            _walk(tr.payload)
    return flow_ids, dsts


def _verdict_block(
    valid: list[EvalUnitSummary], malicious_units_total: int
) -> dict[str, float | int | None]:
    """Unit-level binary-verdict confusion over valid renderings.

    Scores the model's `verdict` (the malicious-vs-benign *call*) against the
    unit's `gold_label`, independent of the per-flow lenses. A unit is a gold
    positive if its label is malicious/mixed; a prediction is positive when
    `verdict == "malicious"`. This captures "did the model get the call right"
    — something the flow-set lenses miss (e.g. `verdict=malicious` with an
    empty index list still scores per-flow F1 1.0 on a benign unit).

    precision/recall/f1 are ``None`` (not the degenerate 1.0) when undefined —
    no predicted positives or no gold positives — so positive-free groups don't
    report spurious perfect scores; only `accuracy` is meaningful there.

    `coverage_adjusted_recall` = TP / (ALL malicious-bearing units, including
    those whose rendering was invalid/forced and thus excluded from `valid`).
    Unlike `recall` it can't be inflated by failing on the hard units — an
    invalid rendering counts as a miss. `malicious_units_total` is its
    denominator.
    """
    tp = sum(1 for g in valid if g.gold_label in _MALICIOUS_GOLD and g.verdict == "malicious")
    fn = sum(1 for g in valid if g.gold_label in _MALICIOUS_GOLD and g.verdict == "benign")
    fp = sum(1 for g in valid if g.gold_label == "benign" and g.verdict == "malicious")
    tn = sum(1 for g in valid if g.gold_label == "benign" and g.verdict == "benign")
    total = tp + fp + tn + fn
    precision = round(tp / (tp + fp), 6) if (tp + fp) else None
    recall = round(tp / (tp + fn), 6) if (tp + fn) else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = 0.0 if (precision is not None and recall is not None) else None
    else:
        f1 = round(2 * precision * recall / (precision + recall), 6)
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "accuracy": round((tp + tn) / total, 6) if total else None,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "malicious_units_total": malicious_units_total,
        "coverage_adjusted_recall": (
            round(tp / malicious_units_total, 6) if malicious_units_total else None
        ),
    }


def _confidence_block(valid: list[EvalUnitSummary]) -> dict[str, float | int]:
    """Confidence-calibration rollup (kept for LOCAL TUNING reference).

    Splits the model's self-reported `confidence` by whether the binary
    verdict was correct, so you can eyeball over/under-confidence. Not a
    headline metric — a coarse signal for tuning prompts/thresholds locally.
    """
    conf = [(g.confidence, g.gold_label in _MALICIOUS_GOLD, g.verdict == "malicious")
            for g in valid if g.confidence is not None and g.verdict is not None]
    correct = [c for (c, gold_pos, pred_pos) in conf if gold_pos == pred_pos]
    wrong = [c for (c, gold_pos, pred_pos) in conf if gold_pos != pred_pos]
    return {
        "n": len(conf),
        "mean": round(_mean([c for (c, _, _) in conf]), 6),
        "mean_correct": round(_mean(correct), 6),
        "mean_incorrect": round(_mean(wrong), 6),
    }


def _native_lens_f1(g: EvalUnitSummary) -> float:
    """The detection-F1 at the granularity native to this unit's structure.

    A unit is scored on the lens that actually discriminates within it:
    ``host_egress`` fan-outs (one internal host → many destinations) use the
    host-level call (``per_host_f1``); ``pair_timeline`` units (a single
    ``src→dst`` over time) use the flow-level call (``per_flow_f1``), since
    their pair/host lenses collapse to ≤1 element. Aggregating this across both
    unit types gives a single efficacy number where each unit is judged at the
    resolution that matters for it, rather than forcing one fixed lens.
    """
    return g.per_host_f1 if g.unit_type == "host_egress" else g.per_flow_f1


def _score_entry(group: list[EvalUnitSummary]) -> dict[str, Any]:
    """Build one (provider, persona) scoring entry.

    Efficacy macros (precision/recall/F1) are computed over VALID renderings
    only — a forced/invalid rendering produces no verdict, so scoring it as an
    empty prediction would credit benign units a perfect 1.0 and fold a
    reliability failure into an efficacy win. Reliability lives in its own
    fields (`first_pass_valid_rate`, `defect_count`) over the FULL group.
    `*_malicious` repeats the macros over malicious/mixed units only, so easy
    benign units don't inflate the headline number.

    Lens relevance varies by unit type: a `pair_timeline` unit is a single
    `(src,dst)`/`src_ip`, so its per-pair and per-host lenses are near-degenerate
    (≤1 element) — per-flow is the universal lens, while per-host is the
    meaningful one for `host_egress` fan-out. The `verdict` block scores the
    binary call independent of the flow set; `confidence` is a local-tuning aid.
    `effective_per_flow_f1` is a blended headline (`per_flow_f1_macro` ×
    `first_pass_valid_rate`) so high efficacy can't be read without reliability.
    `native_lens_f1` scores each unit on the lens native to its structure
    (host_egress→per_host, pair_timeline→per_flow) and macro-averages — a single
    efficacy number that judges every unit at the resolution that matters for
    it, with an `effective_` reliability-blended sibling.
    """
    valid = [g for g in group if g.unit_first_pass_valid]
    mal = [g for g in valid if g.gold_label in _MALICIOUS_GOLD]
    # Full count of malicious-bearing units (valid OR not) — denominator for the
    # coverage-adjusted recall, so failing on hard units can't be hidden.
    malicious_units_total = sum(1 for g in group if g.gold_label in _MALICIOUS_GOLD)
    per_flow_f1_macro = round(_mean([g.per_flow_f1 for g in valid]), 6)
    native_lens_f1 = round(_mean([_native_lens_f1(g) for g in valid]), 6)
    first_pass_valid_rate = round(
        _mean([1.0 if g.unit_first_pass_valid else 0.0 for g in group]), 6
    )
    return {
        "units": len(group),
        "units_scored": len(valid),
        "per_flow_f1_macro": per_flow_f1_macro,
        "effective_per_flow_f1": round(per_flow_f1_macro * first_pass_valid_rate, 6),
        # Each unit scored on its native lens (host_egress→per_host,
        # pair_timeline→per_flow); the combined-across-unit-types efficacy view.
        "native_lens_f1": native_lens_f1,
        "effective_native_lens_f1": round(native_lens_f1 * first_pass_valid_rate, 6),
        "per_pair_f1_macro": round(_mean([g.per_pair_f1 for g in valid]), 6),
        "per_host_f1_macro": round(_mean([g.per_host_f1 for g in valid]), 6),
        "per_flow_precision_macro": round(
            _mean([g.per_flow_precision for g in valid]), 6
        ),
        "per_flow_recall_macro": round(_mean([g.per_flow_recall for g in valid]), 6),
        "malicious_units_scored": len(mal),
        "per_flow_f1_macro_malicious": round(_mean([g.per_flow_f1 for g in mal]), 6),
        "per_pair_f1_macro_malicious": round(_mean([g.per_pair_f1 for g in mal]), 6),
        "per_host_f1_macro_malicious": round(_mean([g.per_host_f1 for g in mal]), 6),
        "verdict": _verdict_block(valid, malicious_units_total),
        "confidence": _confidence_block(valid),
        "first_pass_valid_rate": first_pass_valid_rate,
        "defect_count": sum(1 for g in group if g.defect is not None),
    }


def compute_summary(
    summaries: list[EvalUnitSummary],
    predictions_path: Path,
    *,
    pricing: PricingTable | None = None,
    provider_models: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Aggregate cost, latency, scoring, and cache savings.

    Emits two latency views per ``(provider, persona)``: ``latency_per_unit_ms``
    (task-level — end-to-end wall time to assess one eval unit, from the
    per-rendering summaries) and ``latency_per_call_ms`` (per provider API call,
    read from ``predictions_raw.jsonl``), each reporting count/mean/p50/p95/max,
    plus token totals. The ``scoring`` block is macro-averaged per
    ``(provider, persona)`` over VALID renderings only: the three flow-set
    lenses (+ malicious-units subset), a unit-level ``verdict`` confusion matrix
    (incl. coverage-adjusted recall), a ``confidence`` calibration aid, and
    full-group reliability fields (``first_pass_valid_rate``, ``defect_count``).
    The ``cache`` block is computed per provider from ``cached_tokens`` and the
    shipped ``pricing.yaml`` rate delta (only when ``pricing`` +
    ``provider_models`` are supplied).
    """
    out: dict[str, Any] = {
        "per_unit_count": len(summaries),
        "per_provider_persona": {},
        "latency_per_unit_ms": {},
        "latency_per_call_ms": {},
        "scoring": {},
    }

    # Cost + wall-time rollups from EvalUnitSummary
    pp_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    # Task-level (per-rendering) end-to-end latency series — one entry per
    # assessed unit, the natural companion to the cost rollup ("how long to
    # assess one unit"), as opposed to the per-API-call series below.
    per_unit_lat: dict[tuple[str, str], list[int]] = {}
    for s in summaries:
        key = (s.provider, s.persona)
        b = pp_buckets.setdefault(
            key,
            {
                "renderings_count": 0,
                "cost_usd": 0.0,
                "wall_time_ms": 0,
                "first_pass_valid_count": 0,
            },
        )
        b["renderings_count"] += s.renderings_count
        b["cost_usd"] += s.cost_usd
        b["wall_time_ms"] += s.wall_time_ms
        per_unit_lat.setdefault(key, []).append(int(s.wall_time_ms))
        if s.unit_first_pass_valid:
            b["first_pass_valid_count"] += 1
    out["per_provider_persona"] = {
        f"{p}/{persona}": {**b, "cost_usd": round(b["cost_usd"], 6)}
        for (p, persona), b in sorted(pp_buckets.items())
    }
    out["latency_per_unit_ms"] = {
        f"{p}/{persona}": _latency_stats(vals)
        for (p, persona), vals in sorted(per_unit_lat.items())
    }

    # Scoring rollups: macro-F1 per lens (valid renderings only), reliability,
    # defect count, and a malicious-units-only macro.
    score_buckets: dict[tuple[str, str], list[EvalUnitSummary]] = {}
    for s in summaries:
        score_buckets.setdefault((s.provider, s.persona), []).append(s)
    out["scoring"] = {
        f"{p}/{persona}": _score_entry(group)
        for (p, persona), group in sorted(score_buckets.items())
    }

    # Latency percentiles + per-provider token totals from per-call rows.
    per_call_lat: dict[str, list[int]] = {}
    tokens_by_provider: dict[str, dict[str, int]] = {}
    if predictions_path.exists():
        for line in predictions_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = f"{row['provider']}/{row['persona']}"
            per_call_lat.setdefault(key, []).append(int(row["wall_time_ms"]))
            tb = tokens_by_provider.setdefault(
                row["provider"], {"prompt_tokens": 0, "cached_tokens": 0}
            )
            tb["prompt_tokens"] += int(row.get("prompt_tokens", 0))
            tb["cached_tokens"] += int(row.get("cached_tokens", 0))
    out["latency_per_call_ms"] = {
        key: _latency_stats(vals) for key, vals in sorted(per_call_lat.items())
    }

    out["cache"] = _cache_block(tokens_by_provider, pricing, provider_models)
    return out


def _cache_block(
    tokens_by_provider: dict[str, dict[str, int]],
    pricing: PricingTable | None,
    provider_models: dict[str, str] | None,
) -> dict[str, Any]:
    """Per-provider cache hit-rate + USD savings.

    ``savings_usd`` is ``cached_tokens × (input_rate − cached_input_rate)``
    per million tokens — i.e. what those tokens *would* have cost at the
    uncached rate, minus what they actually cost cached. Zero when pricing
    isn't supplied or the model has no rate entry.
    """
    per_provider: dict[str, Any] = {}
    total_cached = 0
    total_prompt = 0
    total_savings = 0.0
    for provider, tb in sorted(tokens_by_provider.items()):
        cached = tb["cached_tokens"]
        prompt = tb["prompt_tokens"]
        denom = cached + prompt
        hit_rate = round(cached / denom, 6) if denom else 0.0
        savings = 0.0
        if pricing is not None and provider_models and provider in provider_models:
            try:
                rate = pricing.rate(provider, provider_models[provider])
                savings = round(cached / 1_000_000.0 * (rate.input - rate.cached_input), 6)
            except KeyError:
                savings = 0.0
        per_provider[provider] = {
            "cached_tokens": cached,
            "prompt_tokens": prompt,
            "hit_rate": hit_rate,
            "savings_usd": savings,
        }
        total_cached += cached
        total_prompt += prompt
        total_savings += savings
    denom = total_cached + total_prompt
    return {
        "hit_rate": round(total_cached / denom, 6) if denom else 0.0,
        "savings_usd": round(total_savings, 6),
        "per_provider": per_provider,
    }


# ---------------------------------------------------------------------------
# Run-id generator (deterministic-ish; the timestamp + uuid suffix prevents
# collisions across same-second invocations).
# ---------------------------------------------------------------------------


def generate_run_id(
    *,
    ablation: AblationTag,
    mode: RunMode,
    providers: list[str],
    dataset_hash: str,
) -> str:
    """A run_id that's human-readable AND collision-resistant."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    providers_tag = "+".join(sorted(providers))
    short_data = dataset_hash[:8]
    suffix = uuid4().hex[:6]
    return f"{ts}_{mode}_{ablation}_{providers_tag}_{short_data}_{suffix}"


__all__ = [
    "AgentLoop",
    "AgentLoopConfig",
    "AgentLoopResult",
    "RunConfig",
    "RunOutcome",
    "Runner",
    "build_user_kickoff_message",
    "compute_summary",
    "generate_run_id",
    "load_eval_units",
    "prepare_run_artifacts",
    "select_eval_units",
]

# Design Optimizations

A running log of methodology and design changes that improve benchmark
fidelity, tractability, cost, or reproducibility. Each entry records the
**problem**, the **change**, and the **effect**. This is a living document —
append a new entry whenever a similar optimization is made; do not rewrite
history.

Format per entry: `Problem → Change → Effect`, plus the files touched.

---

## 1. Label-agnostic `host_egress` promotion (ground-truth leak fix)

- **Problem.** A source IP was promoted from `pair_timeline` to `host_egress`
  only when it fanned out to enough *malicious* destinations. That used ground
  truth at unit-construction time: the unit *type* itself became a malicious
  signal, and zero benign `host_egress` units ever existed. A model could "win"
  by treating every `host_egress` unit as malicious.
- **Change.** Promotion now keys on **total distinct destinations** (label
  agnostic), regardless of maliciousness. Real benign fan-out (e.g. a busy
  server) now produces benign `host_egress` units.
- **Effect.** All six `(unit_type, gold_label)` strata populate, including
  `host_egress:benign`. The unit type no longer leaks the answer.
- **Files.** `src/socbench/index.py` (`assign_eval_units`).

## 2. Runtime ground-truth safety net for tools

- **Problem.** Tools must never surface label-derived fields, but a new field
  name could slip through and leak the answer to the model.
- **Change.** Tools strip a frozen denylist of ground-truth field names
  (`GROUND_TRUTH_FIELDS`), expanded to cover `attack_type`, `numeric_label`,
  `_numeric_label`, etc. A canary test asserts the set.
- **Effect.** Defense-in-depth: even a careless tool addition cannot expose
  labels at runtime.
- **Files.** `src/socbench/tools/base.py`, `tests/test_tools.py`.

## 3. Time-based unit splitting bounded by `max_flows_per_unit`

- **Problem.** `host_egress` units over a long window could contain thousands
  of flows — intractable for an LLM to reason over, blowing context and cost,
  and synthetic captures inflate this further.
- **Change.** Both `pair_timeline` and `host_egress` units are split into
  contiguous, time-ordered sub-windows bounded by `max_flows_per_unit` (default
  1000); `host_egress_window_minutes` lowered 15 → 5. Time-based splitting was
  chosen over a distinct-destination cap so sub-units keep a clear, contiguous
  scope and cross-search across destinations still works.
- **Effect.** Units stay within model context budgets while preserving unit
  semantics; deterministic chunking keeps the index content-addressable.
- **Files.** `src/socbench/index.py`, `src/socbench/config.py`,
  `config/benchmark_config.yaml`.

## 4. Destination shorthand for `host_egress` enumeration

- **Problem.** On `host_egress` units the model reliably got the **verdict**
  right but returned **zero** `malicious_flow_indices` (it formed a host-level
  judgment instead of enumerating hundreds of flow ids). This produced
  `verdict_indices_mismatch` defects and drove per-flow recall to ~0 — the
  per-flow lens was measuring "did the model bother to enumerate," not detection
  skill. Confirmed at scale: 18/24 defects, per-flow F1 0.10 on an all-
  `host_egress` sample.
- **Change.** Added an optional `malicious_destinations: list[str]` to the
  `submit_assessment` contract. For fan-out units the model may name malicious
  **destination IPs** instead of every flow id; the scorer expands each
  destination to its in-scope flows (unioned with explicit indices) before
  computing all three lenses. A `malicious` verdict backed only by destinations
  is no longer a defect. `dst_ip` is routing metadata, not a label, so this
  stays within the scorer's ground-truth boundary. Does not apply to
  `pair_timeline` (single destination = the unit itself; per-flow enumeration
  stays correct there, especially for `mixed` pairs).
- **Effect.** Verified on an 8-unit Anthropic smoke (same units, before →
  after): `host_egress` defects 2 → 0, per-flow F1 0.50 → 0.875, recall
  0.625 → 0.875, precision 0.875 → 1.0. Rationales confirm real usage — e.g. on
  a `mixed` fan-out unit the model named only the IRC C2 destination as
  malicious and called the NTP destinations benign, expanding to exactly the 62
  malicious flows (per-flow precision = recall = 1.0). Claiming a destination
  whose flows are partly benign still costs precision, as intended.
- **Files.** `src/socbench/models.py`, `src/socbench/scoring.py`,
  `src/socbench/agent.py`, `src/socbench/tools/catalog/submit_assessment.py`,
  `config/prompts/playbook_common.md`.

## 5. Two-class verdict, three-class gold label

- **Problem.** Units genuinely span both benign and malicious flows (`mixed`),
  but the LLM (and the LogLM baseline it is compared against) emit a binary
  call.
- **Change.** The LLM `Verdict` is 2-class (`benign`/`malicious`); the eval-unit
  `GoldLabel` is 3-class (`benign`/`malicious`/`mixed`) and used only for
  stratification and diagnostics. Per-flow/-pair/-host F1 are computed against
  the seeded gold flow set, so a `mixed` unit is scored on *which* flows the
  model caught — comparable to a binary baseline at the flow level.
- **Effect.** Keeps the output contract simple and baseline-comparable while
  still stress-testing mixed units.
- **Files.** `src/socbench/models.py`, `src/socbench/scoring.py`.

## 6. Optional temperature (newer-model compatibility)

- **Problem.** Newer models (e.g. `claude-opus-4-7`) reject an explicit
  `temperature` parameter, returning HTTP 400.
- **Change.** `AdapterRequest.temperature` is optional; adapters omit it from
  the API call when unset. Temperature is plumbed per-provider from config.
- **Effect.** The same harness runs across older and newer models without
  per-model code changes.
- **Files.** `src/socbench/providers/*`, `config/benchmark_config.yaml`.

## 7. Reliability as a first-class axis (strict, no-repair)

- **Problem.** Efficacy alone hides models that emit malformed answers or only
  finish under a forced budget cap.
- **Change.** `unit_first_pass_valid` requires a strictly schema-valid
  `submit_assessment` produced *without* hitting a budget cap (no client-side
  repair); forced-final answers are excluded. `defect_count` flags
  verdict/evidence contradictions. Both roll up per `(provider, persona)` in
  `summary.json`.
- **Effect.** Reliability is measured and reported alongside efficacy, cost, and
  latency rather than being implicit in the F1 numbers.
- **Files.** `src/socbench/agent.py`, `src/socbench/scoring.py`.

## 8. Task-level latency alongside per-call latency

- **Problem.** Per-API-call latency alone does not answer "how long to assess
  one unit," which is what pairs with the cost rollup.
- **Change.** `summary.json` emits both `latency_per_unit_ms` (end-to-end
  per-rendering p50/p95/max) and `latency_per_call_ms` (per provider call).
- **Effect.** Latency can be read at the same granularity as cost.
- **Files.** `src/socbench/agent.py` (`compute_summary`).

## 9. Budget-robust (round-robin) sampling order

- **Problem.** Selected units were emitted sorted by stratum, and the Runner is
  unit-outer (`for unit: for persona: for provider:`) with a clean cost-cap
  abort between units. A run truncated by the cap would therefore finish one
  whole stratum before touching the next — a partial run could cover only 1–2 of
  the six strata, making it unrepresentative.
- **Change.** The final selection order is a deterministic **round-robin across
  strata** (one unit per stratum in rotation, in the fixed `ALL_STRATA` order;
  each stratum keeps its seeded selection order). Any prefix of the list is
  balanced across strata. The selected *set* is unchanged — only the order — so
  scoring and reproducibility are unaffected.
- **Effect.** A cost-capped partial run yields balanced strata coverage. Example
  (993-unit `full` sample): the first 200 units span all six strata (~49 each
  for the large strata) instead of just the first one or two.
- **Files.** `src/socbench/sampling.py`.

## 10. Gemini-safe tool schema sanitization

- **Problem.** The tool `args_schema` carries `additionalProperties: false`, which
  OpenAI strict mode requires and Anthropic tolerates, but Gemini's OpenAPI-subset
  schema dialect rejects with `400 INVALID_ARGUMENT` ("Unknown name
  `additional_properties`"). Every Gemini tool call failed (`adapter_fatal`), so a
  full Gemini run produced zero valid renderings while Anthropic/OpenAI ran fine.
- **Change.** The Gemini adapter recursively strips unsupported keys
  (`additionalProperties`, `$schema`) from each function declaration's parameters
  before sending. OpenAI/Anthropic keep the original schema unchanged.
- **Effect.** The same tool contract runs across all three providers; Gemini tool
  calls succeed instead of 400ing.
- **Files.** `src/socbench/providers/gemini_adapter.py`.

## 11. Async parallel runner

- **Problem.** `Runner.run` was a strictly sequential `for unit: for persona: for
  provider:` loop — each rendering's multi-turn agent loop ran to completion
  before the next started, so exactly one LLM API request was ever in flight.
  A full run (993 units × 4 personas ≈ 3,972 renderings) took tens of hours and
  overran the 24h Cloud Run cap; the configured `max_concurrency` was never read.
  Renderings are almost entirely network-wait, so they're a textbook fit for
  cooperative concurrency.
- **Change.** Adapters expose `async def invoke` over the async SDK clients
  (`AsyncOpenAI`, `AsyncAnthropic`, `genai.Client().aio.models.generate_content`),
  and `AgentLoop.run` / `_invoke_with_retry` / `_dispatch_tool` are async. The
  Runner builds one coroutine per `(unit, persona, provider)` on a single event
  loop, bounds each provider with an `asyncio.Semaphore` (= `max_concurrency`),
  and drains `asyncio.as_completed` — performing *all* artifact writes and
  accumulation on the orchestrating coroutine (single-writer, lock-free;
  incremental JSONL checkpoints intact). Retry backoff is `await asyncio.sleep`
  (frees the loop) and the sync DuckDB tools run via `asyncio.to_thread` so they
  never block it. The cost-budget abort is *soft*: it cancels not-yet-started
  tasks (parked on the semaphore, so no API work is wasted) and lets in-flight
  ones finish. Thin `invoke_sync` / `run_sync` wrappers (`asyncio.run`) preserve
  the synchronous surface for the CLI and tests. Providers default to
  concurrency 1, which keeps the stateful mock adapter correct.
- **Effect.** Near-linear speedup up to each provider's rate limit (e.g. ~30h →
  ~4h at concurrency 8), with a light footprint — thousands of renderings can be
  outstanding on one thread, bounded by rate limits rather than a thread budget.
  Artifact contents are unchanged; only the append order differs.
- **Files.** `src/socbench/providers/base.py`, `…/mock_adapter.py`,
  `…/openai_adapter.py`, `…/anthropic_adapter.py`, `…/gemini_adapter.py`,
  `src/socbench/agent.py`, `src/socbench/cli.py`,
  `config/benchmark_config.yaml`.

## 12. Retry-After backoff + per-provider circuit breaker

- **Problem.** Two failure modes wasted wall-clock under load. (1) On HTTP 429 the
  agent loop slept a blind exponential backoff (capped 8s) and ignored the
  provider's `Retry-After` hint. (2) A provider that was hard-down or
  quota-exhausted (e.g. OpenAI's sustained 429s) kept getting fresh renderings
  for hours, each retrying then failing — burning the whole budget/wall-clock for
  zero usable output.
- **Change.** (1) `RetryableAdapterError` now carries `retry_after_seconds`; the
  OpenAI/Anthropic adapters populate it from the rate-limit response header, and
  `_invoke_with_retry` waits `max(backoff, retry_after)` capped at 30s. (2) The
  Runner tracks *consecutive* fatal renderings per provider; after
  `providers.<name>.circuit_breaker_threshold` (default 12) it logs
  `provider_circuit_open` and cancels that provider's not-yet-started renderings
  while leaving other providers running. `RenderingResult.adapter_fatal` carries
  the signal; the counter lives on the single orchestrating coroutine (no lock).
- **Effect.** Throttled providers back off on the server's terms; a dead provider
  is shed in seconds instead of grinding for hours, so the run finishes (or aborts
  cleanly) far sooner.
- **Files.** `src/socbench/providers/base.py`,
  `src/socbench/providers/anthropic_adapter.py`,
  `src/socbench/providers/openai_adapter.py`, `src/socbench/agent.py`,
  `src/socbench/models.py`, `src/socbench/config.py`, `src/socbench/cli.py`.

## 13. Per-provider `max_output_tokens` wiring

- **Problem.** `AgentLoopConfig.max_output_tokens` was hardcoded to its 1500
  default — the `providers.<name>.max_output_tokens` config value was never
  applied, so output length (a per-call latency + cost driver) couldn't be tuned.
- **Change.** The Runner now threads each provider's configured
  `max_output_tokens` into the agent loop. Unset → unchanged 1500 default.
- **Effect.** Output length is tunable per provider; lowering it cuts per-call
  latency and cost with no other behavior change.
- **Files.** `src/socbench/agent.py`, `src/socbench/cli.py`.

## 14. `max_output_tokens` raised 1500 → 2048 (truncation fix)

- **Problem.** Auditing per-call output tokens across all runs showed Anthropic
  Opus pinned at exactly 1500 on 5 calls — every one the final
  `submit_assessment`, every one `final_valid=False`. The 1500 default was
  silently truncating valid verdicts mid-JSON, concentrated on large
  `host_egress` units whose payload (rationale + `malicious_flow_indices`) is
  longest. Anthropic output p99 was already 1259; Gemini's *visible* output is
  small (≤365) but thinking tokens — which count against the cap for 2.5-series
  — pushed its combined total to ~1149 (≈77% of 1500) even on easy units.
- **Change.** Raised `max_output_tokens` to 2048 for all three providers in
  `config/benchmark_config.yaml`. Raising the ceiling is cost-neutral (billing
  is on tokens *generated*, not the cap) and runaway turns stay bounded by
  `cost_usd_cap_per_rendering` + `max_turns`; the only behavior change is fewer
  truncations.
- **Effect.** A clean smoke (sample, soc_analyst) at 2048 ran 8/8 `final_valid`
  on OpenAI, Anthropic, and Gemini with zero truncation and nothing within ~700
  tokens of the cap (combined max: Gemini 1309, OpenAI 1142, Anthropic 565).
  The sample's units are small, so the `host_egress` truncation fix itself is
  validated only on the `benchmark-v0` dataset.
- **Files.** `config/benchmark_config.yaml`.

## 15. 2048-cap stress test on `benchmark-v0` (all personas × 3 providers)

- **Run.** `20260603T074708Z_smoke_main_anthropic+gemini+openai_4bc5181b` — 96
  renderings, $12.21, ~59 min. The first all-persona run on the large dataset,
  built to stress the 2048 cap on the `host_egress` units that truncated at 1500.
- **Verdict on 2048.** Anthropic: 32/32 valid, largest `submit_assessment` 1319
  tokens, 0 truncations — the original failure mode is fixed. Gemini: 31/32 valid;
  thinking spiked to ~1405 and hit the 2048 ceiling on 3 calls (1 truncated
  submit). Kept at 2048 — 31/32 is acceptable and bumping further trades cost for
  a rare miss. So 2048 resolves the truncation it was chosen to fix.
- **Surfaced two unrelated issues** (see #16, #17).

## 16. OpenAI reasoning tokens were double-counted (cost fix)

- **Problem.** The Responses API reports `usage.output_tokens` as the FULL output
  count with reasoning tokens included as a subset (verified: 0/229 rows had
  `reasoning > output`). The adapter stored `output_tokens=output` (incl.
  reasoning) AND `reasoning_tokens` separately, so the provider-agnostic cost
  formula (`output*output_rate + reasoning*reasoning_rate`, with the two rates
  equal) billed reasoning twice. On the stress run OpenAI was overcharged $1.35 of
  $7.22 (~19%); true cost ~$5.87.
- **Change.** `_extract_openai_usage` now stores `output_tokens = output -
  reasoning` (the visible portion), matching Gemini where candidates/thoughts are
  already disjoint. Cost and the reported output-token column are now correct;
  `finish_reason` (truncation detection) is content-derived and unaffected.
  Historical OpenAI costs were left as-is (no re-backfill).
- **Files.** `src/socbench/providers/openai_adapter.py`,
  `tests/test_providers_openai.py`.

## 17. Per-provider loop-budget multiplier (`budget_multiplier`)

- **Problem.** OpenAI's 19/32 valid on the stress run was NOT truncation — its max
  output was 1207 (< 2048). It exhausts the persona loop budget: ~7.2 turns/render
  and ~55 s vs ~4.8 turns / ~28 s for peers, so on large `host_egress` units it
  hits `max_turns` (10 renderings) or the per-rendering cost cap (3) and is forced
  into invalid submissions. Persona budgets are sized for typical models; a
  chattier reasoning model needs more room without inflating budgets for everyone.
- **Change.** Added `providers.<name>.budget_multiplier` (default 1.0), threaded
  through the Runner. When >1.0 it scales that provider's `max_turns`,
  `max_tool_calls`, `wall_clock_seconds`, and per-rendering cost cap. Set OpenAI to
  1.5; Anthropic/Gemini stay at 1.0.
- **Effect.** OpenAI can complete multi-turn `host_egress` investigations instead
  of being force-submitted; other providers are unchanged.
- **Files.** `src/socbench/config.py`, `src/socbench/agent.py`,
  `src/socbench/cli.py`, `config/benchmark_config.yaml`.

## 18. Metric-path audit fixes (wall time, efficacy, latency)

A pass over the metric calculation paths confirmed the confusion math, cost
rollup, and cache savings are correct, and fixed four interpretation issues:

- **Wall time was a mislabel.** `total_wall_time_ms` summed each rendering's wall
  time, but renderings run concurrently — so it read ~9× the real run duration
  (e.g. 65 min reported vs ~7 min actual). Split into `elapsed_wall_ms` (true
  end-to-end, one `monotonic()` around the run) and `aggregate_rendering_wall_ms`
  (the old sum, sequential-equivalent compute). `RunOutcome` + CLI output updated.
- **Invalid renderings inflated efficacy.** A forced/invalid rendering has no
  verdict → empty predictions; combined with the `(0,0,0)→1.0` per-unit
  convention, benign units scored a perfect 1.0 on a *failure*. Efficacy macros
  now aggregate over VALID renderings only (`units_scored` reported); the failure
  shows up only in the reliability fields (`first_pass_valid_rate`,
  `defect_count`), which still cover the full group.
- **Macro-F1 masked detection by easy benign units.** Added a malicious-units
  subset (`per_{flow,pair,host}_f1_macro_malicious`, `malicious_units_scored`)
  over gold-label malicious/mixed units, so the headline isn't dominated by
  benign units that only test for false positives.
- **Latency now reports `mean`.** Both `latency_per_unit_ms` and
  `latency_per_call_ms` add a `mean` alongside p50/p95/max. These remain
  wall-clock under concurrency (latency under load); a clean isolated number
  needs a `max_concurrency: 1` run — documented in `_latency_stats`.
- **Files.** `src/socbench/agent.py`, `src/socbench/cli.py`.

## 19. Verdict confusion + confidence calibration in scoring

From the evaluation-path review: the three flow-set lenses scored *which flows*
the model flagged but never the binary *call* itself, so `verdict=malicious`
with an empty index list still scored per-flow F1 1.0 on a benign unit.

- **Verdict confusion (headline).** Each `(provider, persona)` scoring entry now
  carries a `verdict` block — `tp/fp/tn/fn` + `accuracy/precision/recall/f1` —
  scoring `verdict` against `gold_label` (malicious/mixed = positive, benign =
  negative) over valid renderings. Reuses `scoring.prf` for consistent degenerate
  conventions. This is the "did it get the call right" metric the lenses missed.
- **Confidence calibration (local-tuning aid).** A `confidence` block logs
  `n / mean / mean_correct / mean_incorrect` (split by verdict correctness). Kept
  for local prompt/threshold tuning, not a headline metric.
- **Lens-relevance note.** Documented in `_score_entry` that `pair_timeline`
  per-pair/per-host lenses are near-degenerate (≤1 element); per-flow is the
  universal lens, per-host the meaningful one for `host_egress`.
- **Files.** `src/socbench/agent.py`, `tests/test_agent_loop.py`.

## 20. Observed-citation clamp (no credit for guessed ids)

- **Problem.** Scoring clamped cited `flow_id`s / `dst_ip`s to the unit's *scope*
  but not to what the model *saw*. The kickoff says "every flow_id you cite must
  appear in a tool response," but nothing enforced it — a model could in principle
  earn precision by naming in-scope ids it never investigated.
- **Change.** `_observed_from_tool_results` harvests every `flow_id`/`dst_ip` from
  this rendering's successful tool responses; `score_unit` now clamps the
  effective predicted set to that observed surface (new optional
  `observed_flow_ids` / `observed_destinations` args; `None` disables for unit
  tests). Gold stays the full in-scope malicious set, so recall still penalises
  missed malicious flows — only unearned *precision* from guesses is removed.
- **Files.** `src/socbench/scoring.py`, `src/socbench/agent.py`,
  `tests/test_scoring.py`.

## 21. Single rendering per unit (known limitation)

Each `(unit × persona × provider)` runs exactly **once** (`renderings_count=1`).
Every metric — the three F1 lenses, the verdict confusion, `first_pass_valid_rate`
— is therefore **single-shot**: there is no resampling or self-consistency, and
no variance estimate, so a flaky result is indistinguishable from a stable one.
This is a deliberate cost trade-off (a full ~1,000-unit pass is already
$200–1,500 per provider). To add confidence intervals later, the lever is N
renderings per unit with majority-vote (verdict) / mean±std (metrics)
aggregation; the run/scoring layout already keys on `rendering_id`, so the
artifacts can carry multiple renderings per unit without schema changes.

## 22. Second eval-path audit — loophole fixes

A re-audit after #18–#21 confirmed those changes closed the lazy-"always
benign", "always malicious", guess-for-precision, and reliability-as-efficacy
loopholes. Three residual issues were fixed:

- **Survivorship bias (efficacy is conditional on validity).** Excluding invalid
  renderings (#18) means a model that only validly answers easy units looks
  better than one that attempts the hard ones. Added
  `verdict.coverage_adjusted_recall` = TP / **all** malicious-bearing units
  (valid or not), with `verdict.malicious_units_total` as denominator — an
  invalid/forced rendering counts as a miss, so it can't be hidden. Read it
  alongside the conditional `recall`.
- **`tools_off` per-flow is structurally zero.** With the observed-citation
  clamp (#20), a model with only `submit_assessment` observes no flow_ids and
  can cite none → per-flow recall 0 by construction. So the flow-lens ablation
  delta is tautological. `aggregate.py` now also emits
  `verdict_accuracy` / `verdict_f1` / `verdict_coverage_adjusted_recall` deltas —
  the meaningful efficacy signal for tool-stripping ablations.
- **Positive-free verdict groups.** `verdict` precision/recall/f1 are now `null`
  (not the degenerate 1.0) when there are no predicted or no gold positives;
  only `accuracy` is reported there. Stops smoke summaries showing spurious 1.0s.
- **Files.** `src/socbench/agent.py`, `src/socbench/aggregate.py`,
  `tests/test_agent_loop.py`.

### Known caveats (recorded, not fixed — correct-by-design or non-exploitable)

- **Brute-force observation isn't an exploit.** A model could dump an id range
  into `get_flows` to mark many flows "observed" and defeat the clamp's intent,
  but tool responses expose *features, not labels*, so it still must classify
  correctly — and `max_tool_calls` / `max_results_cap` / per-rendering budget
  bound it (can't enumerate a 1000-flow `host_egress`). Inefficiency, not gaming.
- **`confidence.mean_correct` / `mean_incorrect` = 0.0 is ambiguous** between
  "no samples in that bucket" and "zero confidence." It's a local-tuning aid, so
  the ambiguity is acceptable; check the `confidence.n` / verdict counts to
  disambiguate.
- **Macro weights every unit equally** regardless of flow count (a 5-flow pair
  counts the same as a 1000-flow `host_egress`). Deliberate — one eval unit is
  one task — and bounded by the per-stratum sampling caps.

## 23. Model selection — evaluated tier parity, kept flagship pin

- **Question.** The lineup mixes tiers: OpenAI `gpt-5.4` (mid, $15 out) vs
  Anthropic `claude-opus-4-7` (flagship, $25 out) vs Gemini `2.5-pro`. We trialed
  pinning Anthropic to balanced-tier `claude-sonnet-4-6` ($3/$15) for closer
  parity with `gpt-5.4`.
- **Finding (smoke run).** Sonnet 4.6 burned its turn/tool budget far faster than
  Opus: 10/32 renderings hit the per-rendering cap and first-pass valid fell to
  68.8% (vs 100% openai / 93.8% gemini), producing an FP-heavy verdict. The swap
  *degraded reliability comparability* rather than improving fairness, so we
  **reverted to `claude-opus-4-7`** as the pinned Anthropic model.
- **Decision.** Keep `claude-opus-4-7` pinned; cost is reported as a result axis,
  not equalised by model selection. `claude-sonnet-4-6` rates remain in
  `pricing.yaml` as a reference for an optional balanced-tier run (which would
  need an Anthropic `budget_multiplier` to keep cap-hits from dominating).
- **Provenance.** `RunMetadata.provider_models` now records the exact model id
  each provider resolved to, so a run's lineup is auditable without
  back-calculating from token costs.
- **Files.** `config/benchmark_config.yaml`, `config/pricing.yaml`,
  `src/socbench/models.py`, `src/socbench/agent.py`.

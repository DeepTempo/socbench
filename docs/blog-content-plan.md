# Blog Content Plan — LogLM vs. Frontier LLM Agents

- **Status:** draft plan (pre-writing)
- **Working title:** *Can Frontier LLM Agents Detect Threats in Raw NetFlow?*
- **Audience:** technical practitioners (detection engineering / threat research / ML), DeepTempo eng blog, dual-layer so a CISO can skim the narrative + figures
- **Tone:** detached, technical, **assertive**; conflict-of-interest stated as a matter-of-fact methods note, not an apology
- **Goal:** a rigorous, reproducible, head-to-head comparison of the purpose-built encoder-only model **LogLM** against frontier reasoning LLMs (GPT, Claude, Gemini) on flow-level threat detection.

---

## Core thesis (what every section drives toward)

**B + D, bounded to this task and dataset:**

1. **B — LogLM wins outright.** Higher detection F1 *and* far lower cost/latency *and* higher reliability — even though the LLMs were given every advantage (agentic tooling, four expert personas, large context budgets, generous dollar caps) and LogLM was given nothing but the raw flows.
2. **D — flow-level detection is structurally the wrong job for general LLMs.** No amount of scaffolding closes the gap, because this is a representation-learning problem (encoder over flow distributions), not an autoregressive-reasoning-over-tables problem.

The claim is **scoped explicitly** to: flow-level malicious detection over benign + Stratosphere malware NetFlow (`benchmark-v0`). Narrowing the claim to exactly what the data supports is what makes the assertive framing defensible.

---

## Fairness architecture (the central integrity decision)

- **Asymmetric by design.** LLMs follow ADR 0002 / `socbench` (full agentic scaffolding). LogLM is inferred single-shot on raw flows. ADR 0001 (single-shot harness for everyone) is **canned**.
- **The asymmetry is a shield, not a liability:** the LLMs got every engineered advantage; LogLM got raw flows only. A LogLM win under that handicap is *conservative*. State this up front.
- **Comparability — like-for-like subset only.** LogLM's whole-corpus predictions are subset to each eval unit's `flow_ids` and scored on the same gold (`_is_malicious`) via `socbench`'s own `score_unit`, so both sides come from identical metric code. Report only where scopes overlap; drop the rest.
- **One disclosed asymmetry:** the observed-citation clamp applies to the LLM agents (they only get credit for flows they actually queried) but not to single-shot LogLM (it sees the whole unit). Name this in a footnote rather than letting a reviewer find it.

---

## Headline metric

- **Headline:** `per_flow_f1_macro_malicious` — macro per-flow F1 over `malicious`/`mixed` units only (overall per-flow F1 is inflated by benign units scoring 1.0).
- Reported alongside the **full per-flow / per-pair / per-host lens table** so nothing looks cherry-picked.
- **`effective_per_flow_f1`** (F1 × first-pass-valid rate) surfaced as the accuracy × reliability composite.
- Binary **verdict confusion** kept as the CISO-facing summary lens.

---

## Section outline (11 parts)

0. **TL;DR + headline figure.** Bounded claim in 3 sentences + COI one-liner + pointer to open harness/artifacts. Drop **F1 (cost-vs-F1 frontier)** here.
1. **The question.** "Here's raw traffic, no alerts, no hints — what's bad?" Why flow-level, no-prior detection is the honest test.
2. **Setup at a glance.** LLMs as budgeted multi-turn agents (tools + 4 personas) vs. single-shot LogLM; lead with the deliberate-handicap framing.
3. **Methodology (deep).** Data & gold; eval units (`pair_timeline` vs `host_egress`, the label-agnostic fan-out rule); persona×tool matrix; the three F1 lenses; reliability (`first_pass_valid`, defects) + cost model; observed-citation clamp; structured output / no-repair.
4. **The fairness contract.** Explicit steelman of the LLMs; like-for-like subset scoring; the one disclosed clamp asymmetry; no-leakage statement.
5. **Results.**
   - (a) detection F1 — headline + full lens table [**F2**]
   - (b) cost & latency / $-per-correct
   - (c) reliability [**F4**]
   - (d) **ablations — does scaffolding even help?** [**F3**] (direct evidence for D)
   - (e) efficiency frontier [**F1**] + `effective_per_flow_f1`
6. **Why purpose-built wins.** Mechanism behind D: encoder representation learning vs. autoregressive reasoning over flow tables; tie back to near-flat ablation deltas.
7. **Threats to validity.** Single dataset, limited families, train/test provenance, the subset scope decision, single-rendering / no-CIs.
8. **Reproducibility.** Pinned models, `tools_manifest_sha`, dataset hash, seeds, pricing snapshot, raw artifacts, "clone it and falsify us."
9. **Takeaways.** Practitioner layer + CISO layer.
10. **Appendix.** Full tables [**T1**], provenance [**T2**], prompts, config hashes.

---

## Figures & tables

| ID | Content |
|----|---------|
| **F1** (headline) | Cost-vs-accuracy frontier: log-scale $/correct-detection (x) vs macro detection-F1 (y), marker size/color = reliability; LogLM alone in the top-left corner |
| **F2** | Grouped bars — detection F1 by lens (flow / pair / host) × model, LogLM vs each LLM's best persona |
| **F3** | Ablation deltas — F1 change from `tools_off` / `playbooks_off` (evidence for D; ideally near-flat) |
| **F4** | Reliability — first-pass-valid rate + defect counts per model/persona |
| **F5** | Case walk-through — one `host_egress` unit: agent turns vs. what LogLM flagged |
| **T1** | Master results table (model × persona × lens F1, cost, latency, reliability) |
| **T2** | Config provenance (pinned models, hashes, seeds, pricing snapshot) |

---

## Reproducibility posture

Full openness: open harness link + raw run artifacts (`predictions_raw.jsonl`, `summary.json`, tool-call logs) + dataset build scripts/provenance + pinned model IDs + `tools_manifest_sha` + dataset hash (`4bc5181b…`) + `sample_seed: 7` + `pricing.yaml` snapshot date. End with an explicit "clone it, plug in your keys, falsify us" invitation. (Confirm Stratosphere/CIC redistribution licensing for the parquet itself.)

---

## Statistics

Point estimates only, leaning on large N (~1,500 units) + open artifacts. **No-CI is acknowledged as an explicit limitation** in §7. Note: bootstrap CIs over the unit population (and a paired Wilcoxon / bootstrap test on per-unit F1 deltas) remain a **free** future upgrade — no new inference required — if a reviewer pushes on uncertainty.

---

## Open pre-publish dependencies (facts to confirm, not decisions)

1. **Join key.** Confirm LogLM's whole-corpus predictions can be subset to eval-unit `flow_ids` (the join key exists).
2. **No leakage.** Confirm LogLM's training provenance vs. `benchmark-v0` for the no-leakage statement.

---

## Pinned config reference (from `config/benchmark_config.yaml`)

- Models: `gpt-5.4` (budget_multiplier 1.5), `claude-opus-4-7`, `gemini-2.5-pro`; `max_output_tokens: 2048`
- `cost_usd_cap_per_rendering: 0.50`
- `host_egress_fanout_K: 10`, `host_egress_window_minutes: 5`, `max_flows_per_unit: 1000`
- Full sampling: `units_per_stratum: 500`, `full_unit_cap: 1500`; strata ≈ he:benign 87 / he:mal 410 / he:mixed 932 / pt:benign 15936 / pt:mal 5 / pt:mixed 1
- Personas (max_turns / max_tool_calls / wall_s): soc_analyst 4/6/60, threat_analyst 8/12/120, adversary_hunter 10/16/150, detection_engineer 12/20/180
- Dataset: `benchmark-v0` (~758k flows, ~37.3% malicious; 4 Stratosphere malware + 4 benign captures)

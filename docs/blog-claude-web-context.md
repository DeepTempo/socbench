# Blog Build Context — LogLM vs. Frontier LLM Agents

> **How to use this document.** Paste it into a new Claude.ai conversation as the first message, then collaborate section by section. It is fully self-contained: Claude does **not** have access to the source repository, so every fact needed to write the post is reproduced here. Treat it as the single source of truth. Where a real result number is required, use the bracketed placeholders (e.g. `[LOGLM_FLOW_F1]`) — **do not invent numbers** (see Guardrails).

---

## 0. The assignment

Write a rigorous, reproducible technical blog post comparing **LogLM** (a purpose-built, encoder-only foundation model for cybersecurity threat detection) against **frontier reasoning LLMs** (OpenAI GPT, Anthropic Claude, Google Gemini) on **flow-level threat detection over raw NetFlow**.

- **Working title:** *Can Frontier LLM Agents Detect Threats in Raw NetFlow?* (question framing; the finding lives in the TL;DR, not the title)
- **Audience:** technical practitioners (detection engineering / threat research / ML). Dual-layer: a CISO should be able to skim the narrative + figures and stop at the limitations section.
- **Tone:** detached, technical, **assertive**. Conclusions are stated plainly and *earned* by methodological rigor and open artifacts — not by rhetorical hedging or hype. Conflict-of-interest (DeepTempo builds LogLM) is disclosed up front as a matter-of-fact methods note.
- **Publisher / conflict of interest:** DeepTempo, the maker of LogLM, is running this benchmark. Credibility comes from: open harness, pinned models, raw artifacts published, and an explicit "clone it and falsify us" invitation.

---

## 1. Core thesis (every section drives toward this)

**Bounded to this task and dataset, two claims:**

- **B — LogLM wins outright.** Higher detection F1 *and* far lower cost/latency *and* higher reliability — even though the LLMs were handed every advantage (agentic tooling, four expert SOC personas, large context budgets, generous dollar caps) and LogLM got nothing but the raw flows.
- **D — flow-level detection is structurally the wrong job for general LLMs.** Scaffolding does not close the gap, because this is a *representation-learning* problem (an encoder recognizing malicious structure across flow distributions), not an *autoregressive-reasoning-over-tables* problem.

**Scope discipline:** the claim is explicitly limited to *flow-level malicious detection over benign + Stratosphere malware NetFlow*. Narrowing the claim to exactly what the data supports is what makes the assertive framing defensible. Do not generalize to "LLMs can't do security."

**The intellectual wedge that makes D feel inevitable:** there are two kinds of "AI for cyber." *Reasoning* tasks (e.g. enumerating a target and chaining an exploit) decompose into narratable steps — autoregressive models excel here. *Recognition* tasks (finding malicious structure in hundreds of thousands of unlabeled flows) have no narrative to follow — there is nothing to reason *through*, only structure to recognize. Detection is the second kind.

---

## 2. What the benchmark is (self-contained technical brief)

**socbench** is a local-first, open-source harness that benchmarks frontier LLMs as **SOC agents** on **raw NetFlow** (network flow records — not application logs). Each model runs a **bounded, multi-turn agent loop** against a deterministic, content-addressed index of flows, investigates using read-only tools, and submits a structured verdict that is scored against gold labels.

**Pipeline (three stages):**
1. **Index** — normalize parquet datasets to a canonical schema, assign stable `flow_id`s, derive IP-pairs / hosts / rollups, and seed **eval units**.
2. **Agent** — for each eval unit × persona × provider, run the multi-turn loop with tools and budgets, ending in a `submit_assessment` call.
3. **Scoring** — compute per-flow / per-pair / per-host F1, reliability, verdict confusion, and cost.

**Eval units** are the scoring boundary. Each unit is one of:
- `pair_timeline` — a single `(src_ip, dst_ip)` conversation, time-ordered.
- `host_egress` — one source IP fanning out to many destinations in a time window (assigned when a source reaches ≥ K=10 distinct destinations in any 5-minute bucket; the rule is **label-agnostic** to avoid leaking ground truth into unit typing).

Units larger than `max_flows_per_unit: 1000` are split into contiguous time sub-windows.

**Gold labels** per unit are derived from per-flow `_is_malicious` and used only by the scorer and the index builder — they are **never** shown to the models. A `gold_label` of `benign` / `malicious` / `mixed` is used only for stratified sampling (`mixed` = some-but-<80% malicious flows).

---

## 3. The two contestants

### LLM agents (the "Method 2" / ADR-0002 path)

- Models (pinned for the run): **OpenAI `gpt-5.4`**, **Anthropic `claude-opus-4-7`**, **Google `gemini-2.5-pro`**. `max_output_tokens: 2048` each.
- Run as **multi-turn agents** with **persona-scoped, read-only tools**. Four personas, each with its own loop budget (turns / tool calls / wall-clock) and tool allowlist:

  | Persona | max_turns | max_tool_calls | wall_clock_s | Tools (beyond the SOC base set) |
  |---|---|---|---|---|
  | soc_analyst | 4 | 6 | 60 | base only |
  | threat_analyst | 8 | 12 | 120 | + `top_destinations`, `pair_stats` |
  | adversary_hunter | 10 | 16 | 150 | + `port_proto_matrix`, `rarity_stats` |
  | detection_engineer | 12 | 20 | 180 | same as hunter |

  Base tools: `list_pairs`, `get_pair_timeline`, `get_flows`, `host_rollup`, `submit_assessment`.
- **Cost cap per investigation:** `$0.50` per rendering (a "rendering" = one eval_unit × persona × provider).
- **Structured output, no client-side repair.** The final answer is a strict JSON schema (`verdict`, `confidence`, `malicious_flow_indices`, `malicious_destinations`, `rationale`) declared natively to each provider. Invalid responses are recorded as failures, not silently fixed.
- **Ground-truth firewall:** tool outputs strip all label fields, so the model never sees `_is_malicious` etc.
- **Observed-citation clamp:** a model only gets credit for flow_ids / destinations it actually saw in a tool response during that investigation — no credit for guessed IDs.
- **Ablations:** `tools_off` (only `submit_assessment` available) and `playbooks_off` (persona playbook removed) isolate how much the scaffolding actually contributes.

### LogLM (the purpose-built baseline)

- Encoder-only foundation model purpose-built for cybersecurity threat detection. Run **single-shot on raw flows** — no tools, no personas, no multi-turn loop, no scaffolding.
- This asymmetry is **deliberate and stated up front**: the LLMs received every engineered advantage; LogLM received only the raw flows. A LogLM win under that handicap is a *conservative* result, not a flattering one.

---

## 4. How LogLM and the LLMs are compared fairly

- **Same gold.** Both sides are scored against the identical per-flow `_is_malicious` labels.
- **Like-for-like subset only.** LogLM produces whole-corpus predictions; for the comparison these are **subset to each eval unit's `flow_ids`** and scored through socbench's *own* `score_unit` code, so both sides' F1 numbers come from identical metric logic. Report only where the scopes overlap. (A whole-corpus LogLM number may be mentioned separately as an "operational deployment" lens, clearly labeled as a different measurement.)
- **One disclosed asymmetry:** the observed-citation clamp applies to the LLM agents but not to single-shot LogLM (which sees the whole unit). Disclose this in a footnote rather than letting a reviewer find it.
- **No leakage:** state LogLM's training provenance relative to the benchmark dataset and confirm no train/test overlap.

---

## 5. Scoring & metrics

**Three F1 lenses** (computed per unit, then macro-averaged across units):
- **per-flow** — predicted-malicious membership vs. gold, flow by flow.
- **per-pair** — a distinct `(src_ip, dst_ip)` is malicious if any of its flows is.
- **per-host** — same, keyed by source IP (the meaningful lens for fan-out `host_egress` units).

**Headline metric:** **`per_flow_f1_macro_malicious`** — macro per-flow F1 over `malicious`/`mixed` units only. (Overall per-flow F1 is inflated by benign units, where an empty prediction on empty gold scores 1.0.) Always report it alongside the full per-flow / per-pair / per-host table so nothing looks cherry-picked.

**Composite:** **`effective_per_flow_f1`** = per_flow_f1_macro × first_pass_valid_rate (accuracy × reliability in one number).

**Reliability:** `first_pass_valid_rate` (fraction of investigations that produced a valid structured answer on the first try, without being forced by a budget cap) and `defect_count` (e.g. verdict/indices mismatches).

**Cost:** dollars per investigation and per correct detection, captured per call (prompt / output / reasoning / cached tokens × pricing snapshot).

**Verdict confusion:** unit-level binary verdict (benign vs malicious) vs gold — the CISO-facing summary lens.

---

## 6. Dataset

- **`benchmark-v0`**: ~**757,641 flows**, ~**37.3% malicious**. Built from **4 Stratosphere malware captures** (`malware_1_1`, `malware_3_1`, `malware_8_1`, `malware_34_1`) + **4 benign captures** (`normal-HTTPS-website`, `normal-at-home-linux`, `normal-university-linux`, `normal-xdsl-linux`), normalized to a canonical NetFlow schema. Per-flow labels.
- Canonical fields: `src_ip`, `dst_ip`, `ts_start`, `protocol`, `src_port`, `dst_port`, `bytes_in/out`, `pkts_in/out`, `tcp_flags`, `flow_duration_ms`, `sampling_rate`.
- **Full-run sampling:** stratified across 6 strata (`{pair_timeline, host_egress} × {benign, malicious, mixed}`), `units_per_stratum: 500`, capped at `full_unit_cap: 1500` units, seeded (`sample_seed: 7`), selected round-robin so cost-capped partial runs stay balanced. The selected set is `host_egress`-heavy (~66%); `pair_timeline` malicious units are scarce.
- CIC-IDS2018 appears only in a tiny smoke `sample`, **not** in the headline full run.

---

## 7. Post structure (11 sections)

0. **TL;DR + headline figure.** Bounded claim in 3 sentences + COI one-liner + pointer to open harness/artifacts. Show **F1 (cost-vs-F1 frontier)**.
1. **The question.** "Here's raw traffic, no alerts, no hints — what's bad?" Why flow-level, no-prior detection is the honest test.
2. **Setup at a glance.** LLMs as budgeted agents (tools + 4 personas) vs. single-shot LogLM; lead with the deliberate-handicap framing.
3. **Methodology (deep).** Data & gold; eval units; persona×tool matrix; the three F1 lenses; reliability + cost model; observed-citation clamp; structured output / no-repair.
4. **The fairness contract.** Explicit steelman of the LLMs; like-for-like subset scoring; the one disclosed clamp asymmetry; no-leakage statement.
5. **Results.** (a) detection F1 — headline + full lens table [F2]; (b) cost & latency / $-per-correct; (c) reliability [F4]; (d) **ablations — does scaffolding even help?** [F3]; (e) efficiency frontier [F1] + effective_per_flow_f1.
6. **Why purpose-built wins.** Mechanism behind D: encoder representation learning vs. autoregressive reasoning over flow tables; tie back to near-flat ablation deltas.
7. **Threats to validity.** Single dataset, limited families, train/test provenance, the subset scope decision, single-rendering / no-CIs.
8. **Reproducibility.** Pinned models, tools-manifest hash, dataset hash, seeds, pricing snapshot, raw artifacts, "clone it and falsify us."
9. **Takeaways.** Practitioner layer + CISO layer.
10. **Appendix.** Full results table [T1], provenance [T2], prompts, config hashes.

---

## 8. Figures & tables

| ID | Content |
|----|---------|
| **F1** (headline) | Cost-vs-accuracy frontier: log-scale $/correct-detection (x) vs macro detection-F1 (y); marker size/color = reliability; LogLM alone in the top-left corner |
| **F2** | Grouped bars — detection F1 by lens (flow / pair / host) × model; LogLM vs each LLM's best persona |
| **F3** | Ablation deltas — F1 change from `tools_off` / `playbooks_off` (evidence for D; ideally near-flat) |
| **F4** | Reliability — first-pass-valid rate + defect counts per model/persona |
| **F5** | Case walk-through — one `host_egress` unit: agent turns vs. what LogLM flagged |
| **T1** | Master results table (model × persona × lens F1, cost, latency, reliability) |
| **T2** | Config provenance (pinned models, hashes, seeds, pricing snapshot) |

---

## 9. The intro (already drafted — use as-is or refine)

> Large language models are the most general computational tool we have built. Trained on internet-scale text and code, they are not programmed for any single task; they are exposed to enough of human knowledge that they can be *asked*, in plain language, to do almost anything — and they will produce a credible attempt. In a few years they have moved from autocomplete to the default interface for legal analysis, software engineering, scientific literature review, and a growing share of the knowledge work that used to require a specialist. The reasonable prior, today, is that if a task can be described, a frontier model can take a real run at it.
>
> That generality is grounded in a specific strength: **reasoning over sequences**. Modern models decompose a problem into steps, hold intermediate state, call tools, read the results, and revise — the loop that lets them debug a program they have never seen or chain a multi-step exploit against a known target. When a task can be *narrated* — when there is a thread to follow from premise to conclusion — these models are extraordinarily capable, and getting more so. It is why the instinct across security, our field included, is to reach for them first.
>
> Threat detection over raw network telemetry is where that instinct meets its hardest test. The input is not a narrative; it is a few hundred thousand NetFlow records — source, destination, ports, bytes, packets, timestamps — with no alerts, no labels, and no story to follow. The signal of compromise is rarely in any single flow; it lives in the *structure* of the traffic, in distributions and rhythms across thousands of connections, against a background of benign activity that looks superficially identical. This is not a problem you reason *through*. It is a problem you have to *recognize*. And whether a general-purpose reasoner can do that as well as a model built specifically for it is an empirical question, not a matter of opinion.
>
> So we measured it. We gave four frontier reasoning models — GPT, Claude, and Gemini — every advantage we could engineer: investigative tools, four expert SOC personas, large context budgets, and a generous per-investigation spend. We gave a purpose-built encoder-only model nothing but the raw flows. Then we asked all of them the same question over the same network traffic: what here is malicious? This is what we found.

---

## 10. Results placeholders (fill from the run; never invent)

When writing results, leave these as bracketed placeholders until the real numbers are pasted in:

- `[LOGLM_FLOW_F1_MAL]`, `[GPT_FLOW_F1_MAL]`, `[CLAUDE_FLOW_F1_MAL]`, `[GEMINI_FLOW_F1_MAL]` — headline per_flow_f1_macro_malicious per model (best persona for the LLMs).
- `[LOGLM_PAIR_F1]` / `[LOGLM_HOST_F1]` and LLM equivalents — other two lenses.
- `[LOGLM_COST_PER_CORRECT]` vs `[LLM_COST_PER_CORRECT]` — cost gap (state the order of magnitude).
- `[LOGLM_LATENCY]` vs `[LLM_LATENCY]` — per-investigation latency.
- `[LLM_FIRST_PASS_VALID_RATE]` per model — reliability.
- `[ABLATION_DELTA_TOOLS]`, `[ABLATION_DELTA_PLAYBOOKS]` — F1 change when scaffolding is removed (the closer to zero, the stronger D).
- `[N_UNITS]` — number of eval units in the comparison (~1,500).

---

## 11. Guardrails for the writer (Claude web)

1. **Never invent numbers.** Use the placeholders in §10. If a number is needed and not provided, ask for it.
2. **Stay bounded.** The claim is about flow-level detection on this dataset. Do not write "LLMs can't do security" or generalize beyond the evidence.
3. **Steelman the LLMs.** They were given every advantage; say so. The strength of the result comes from beating *well-configured* opponents, not weak ones.
4. **No hype, no marketing adjectives.** Detached and assertive. Let deltas and the open artifacts carry the verdict.
5. **Disclose the COI early** and treat reproducibility ("clone it and falsify us") as the credibility mechanism.
6. **Honor the open methodology seams:** the subset-scope decision and the clamp asymmetry must be stated, not hidden.
7. **Acknowledge limitations honestly:** single dataset, limited malware families, point estimates / no confidence intervals (note that bootstrap CIs over the unit population are a cheap future upgrade requiring no new inference), single rendering per unit.
8. **Keep two readable layers:** a narrative spine a CISO can skim, and the technical depth a detection engineer can verify.

---

## 12. Pinned config quick reference

- Models: `gpt-5.4` (loop-budget ×1.5, since it is chattier/slower), `claude-opus-4-7`, `gemini-2.5-pro`; `max_output_tokens: 2048`.
- `cost_usd_cap_per_rendering: 0.50`.
- `host_egress_fanout_K: 10`, `host_egress_window_minutes: 5`, `max_flows_per_unit: 1000`.
- Full sampling: `units_per_stratum: 500`, `full_unit_cap: 1500`, `sample_seed: 7`.
- Dataset: `benchmark-v0` (~757,641 flows, ~37.3% malicious; 4 Stratosphere malware + 4 benign captures).
- Rendering flow caps per provider (large-unit splitting): Claude 600 / GPT 1200 / Gemini 3000.

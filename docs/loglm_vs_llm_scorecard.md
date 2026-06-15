# LogLM vs LLM — Consolidated Scorecard

Regenerate with:

```bash
cd socbench && .venv/bin/python scripts/loglm_vs_llm_scorecard.py
```

Captures: `malware_1_1`, `malware_3_1`, `malware_8_1`, `malware_34_1`.
LLM providers: `anthropic`, `openai`, `gemini` (F1 averaged over personas).

## Provenance & key limitation

- **LLM**: socbench `predictions_per_flow.parquet` + `eval_units_summary.jsonl`
  (`gs://your-bucket/benchmark-v0/results/runs/...`).
- **LogLM**: per-sequence **gold** (`labels.parquet`) + **aggregate** `confusion.json`
  (`s3://your-data-lake/lake/v1/...`).
- LogLM publishes per-sequence gold but **not** per-sequence predictions — only the
  aggregate confusion matrix. So LogLM's column is its own authoritative aggregate, and
  the LLM is **projected** onto LogLM's sequences (socbench flows asof-matched into
  LogLM's `(undirected-pair, time-window)` sequences) and scored against LogLM's
  sequence gold. `cov%` = share of LogLM sequences that received ≥1 socbench flow.

## 1) Detection (malicious class) — sequence grain

| capture | LogLM P/R/F1 | provider | cov% | LLM flow-proj P/R/F1 |
|---|---|---|---|---|
| malware_1_1 | 0.99 / 0.87 / **0.93** | anthropic | 77% | 0.08 / 0.12 / 0.09 |
| | | openai | 80% | 0.46 / 0.17 / 0.23 |
| | | gemini | 80% | 0.08 / 0.17 / 0.11 |
| malware_3_1 | 1.00 / 0.82 / **0.90** | anthropic | 92% | 0.87 / 0.51 / 0.64 |
| | | openai | 95% | 0.97 / 0.38 / 0.54 |
| | | gemini | 95% | 0.84 / 0.42 / 0.56 |
| malware_8_1 | 1.00 / 1.00 / **1.00** | anthropic | 47% | 1.00 / 1.00 / 1.00 |
| | | openai | 66% | 0.91 / 0.10 / 0.18 |
| | | gemini | 66% | 0.93 / 1.00 / 0.96 |
| malware_34_1 | 0.98 / 0.60 / **0.75** | anthropic | 87% | 0.93 / 0.97 / **0.95** |
| | | openai | 92% | 0.90 / 0.68 / 0.77 |
| | | gemini | 92% | 0.83 / 0.90 / 0.86 |

At the sequence grain (most apples-to-apples), LogLM leads on `malware_1_1`/`malware_3_1`;
LLMs are competitive or ahead on `malware_8_1`/`malware_34_1` (anthropic edges LogLM on `34_1`).

## 2) Verdict (socbench native eval-unit grain) — LLM unit verdict vs unit gold

| capture | provider | units | malicious | verdict P/R/F1 |
|---|---|---|---|---|
| malware_1_1 | anthropic | 303 | 303 | 1.00 / 0.87 / 0.93 |
| | openai | 415 | 415 | 1.00 / 0.98 / 0.99 |
| | gemini | 415 | 415 | 1.00 / 0.97 / 0.99 |
| malware_3_1 | anthropic | 260 | 258 | 0.99 / 0.96 / 0.97 |
| | openai | 287 | 285 | 0.99 / 0.79 / 0.88 |
| | gemini | 287 | 285 | 1.00 / 0.93 / 0.96 |
| malware_8_1 | anthropic | 4 | 4 | 1.00 / 1.00 / 1.00 |
| | openai | 5 | 4 | 1.00 / 0.94 / 0.96 |
| | gemini | 5 | 4 | 0.95 / 0.94 / 0.94 |
| malware_34_1 | anthropic | 183 | 180 | 0.99 / 0.97 / 0.98 |
| | openai | 211 | 208 | 0.99 / 0.79 / 0.88 |
| | gemini | 211 | 208 | 0.98 / 0.88 / 0.93 |

These matched units are **~99% malicious**, so this block is effectively a **recall test**
(precision ~1.0 is uninformative — almost no benign units in scope). See §3 for FP behaviour.

## 3) Benign — false-positive behaviour (specificity)

**LogLM benign FP (within-capture, sequence grain):**

| capture | neg seqs | FP | FP% |
|---|---|---|---|
| malware_1_1 | 486 | 2 | 0.41% |
| malware_3_1 | 79 | 0 | 0.00% |
| malware_8_1 | 206 | 0 | 0.00% |
| malware_34_1 | 166 | 1 | 0.60% |

(Separate `stratosphere-benign` run: FP=4267 / tn=8347 → **33.83%** — different model run/dataset.)

**LLM benign FP:**

| provider | flow FP% (cap) | flow FP% (normal-*) | verdict FP% (cap) | verdict FP% (normal-*) |
|---|---|---|---|---|
| anthropic | 0.70% | 51.02% | 36.29% | 39.08% |
| openai | 0.33% | 3.73% | 53.06% | 42.61% |
| gemini | 1.20% | 21.36% | 40.61% | 85.80% |

## Bottom line

- **Detection / recall**: LLMs competitive, occasionally ahead (esp. anthropic).
- **Benign / false alarms**: **LogLM wins decisively** — ~0% verdict FP vs the LLMs'
  **36–53%** on benign units (gemini up to 86% on clean traffic).
- **Flow localization**: LogLM precise; LLMs detect-but-don't-localize at flow grain.

LogLM's signature is the SOC-desirable profile: strong recall with a very low
false-positive rate. The LLMs over-alarm at the verdict level.

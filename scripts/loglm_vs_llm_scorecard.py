#!/usr/bin/env python3
"""
Consolidated LogLM vs LLM scorecard.

Regenerates, in one pass, the three comparison lenses we care about:
  1. DETECTION  (malicious class) -- LogLM vs LLM at LogLM's sequence grain
  2. VERDICT    (socbench native) -- LLM unit-verdict detection on the 4 captures
  3. BENIGN     (false-positive behaviour) -- LogLM vs LLM specificity

------------------------------------------------------------------------------
INPUTS (provenance)
------------------------------------------------------------------------------
LLM (socbench) runs -- mirrored locally under /tmp/runs/:
    {provider}.parquet        <- predictions_per_flow.parquet
                                  (gs://your-bucket/benchmark-v0/results/runs/...)
    {provider}_units.jsonl     <- eval_units_summary.jsonl  (unit verdicts + gold)
Source flows (for flow_id -> source/IP/ts reconstruction):
    socbench/data/benchmark-v0.parquet  (the canonical benchmark-v0 flow table)
LogLM artifacts -- mirrored locally under /tmp/loglm/{capture}/:
    labels.parquet            <- per-sequence GOLD label  (s3://your-data-lake/lake/v1/labels/...)
    confusion.json            <- LogLM's AGGREGATE confusion matrix per capture
                                  (s3://your-data-lake/lake/v1/eval/...)

IMPORTANT LIMITATION
    LogLM publishes per-sequence GOLD (labels.parquet) but NOT per-sequence
    PREDICTIONS. Its only published prediction signal is the aggregate
    confusion.json. Therefore:
      * LogLM's column = its own authoritative aggregate (tp/fp/tn/fn).
      * The LLM is *projected* onto LogLM's sequences (socbench flows asof-matched
        into LogLM's (undirected-pair, time-window) sequences) and scored against
        LogLM's sequence gold.
      * LLM sequence coverage (% of LogLM sequences that received >=1 socbench
        flow) is reported so the population gap is explicit.
------------------------------------------------------------------------------
"""
import json
import statistics as st

import polars as pl

CAPS = ["malware_1_1", "malware_3_1", "malware_8_1", "malware_34_1"]
PROVIDERS = ["anthropic", "openai", "gemini"]
RUNS = "/tmp/runs"
LOGLM = "/tmp/loglm"
SRC_PARQUET = "data/benchmark-v0.parquet"

# deterministic flow_id sort keys (must match socbench/src/socbench/index.py)
SORT = ["ts_start", "src_ip", "dst_ip", "src_port", "dst_port",
        "protocol", "bytes_out", "pkts_out"]


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def upk(a, b):
    return pl.when(a <= b).then(a + "|" + b).otherwise(b + "|" + a)


# ---------------------------------------------------------------- flow index
def load_flow_index() -> pl.DataFrame:
    raw = pl.read_parquet(SRC_PARQUET).with_columns([
        pl.col("src_port").cast(pl.Int64),
        pl.col("dst_port").cast(pl.Int64),
        pl.col("ts_start").cast(pl.Float64),
        pl.col("bytes_out").cast(pl.Float64),
        pl.col("pkts_out").cast(pl.Float64),
    ])
    f = (raw.sort(SORT)
            .with_row_index("flow_id")
            .select(["flow_id", "source", "src_ip", "dst_ip", "ts_start"]))
    return f.with_columns([
        (pl.col("ts_start") * 1000.0).alias("ts_ms"),
        upk(pl.col("src_ip"), pl.col("dst_ip")).alias("upk"),
    ])


# ----------------------------------------------- LogLM sequence (gold) index
def load_loglm_windows() -> pl.DataFrame:
    rows = []
    for cap in CAPS:
        lb = pl.read_parquet(f"{LOGLM}/{cap}/labels.parquet")
        parts = lb["sequence_id"].str.split("_")
        lb = lb.with_columns([
            parts.list.get(0).alias("ip_a"),
            parts.list.get(1).alias("ip_b"),
            parts.list.get(2).cast(pl.Float64).alias("win_start"),
            pl.lit(cap).alias("cap"),
        ])
        lb = lb.with_columns(upk(pl.col("ip_a"), pl.col("ip_b")).alias("upk"))
        rows.append(lb.select(["sequence_id", "cap", "upk", "win_start",
                               "malicious"]).rename({"malicious": "seq_gold"}))
    return pl.concat(rows)


def match_flows_to_sequences(flows: pl.DataFrame,
                             wins: pl.DataFrame) -> pl.DataFrame:
    """asof-match each socbench flow to the nearest preceding LogLM window
    start within the same undirected IP pair -> assigns sequence_id + seq_gold."""
    f = flows.sort("ts_ms")
    w = wins.sort("win_start")
    m = f.join_asof(w, left_on="ts_ms", right_on="win_start", by="upk",
                    strategy="backward")
    return m.filter(pl.col("sequence_id").is_not_null())


# --------------------------------------------------------------- LLM loaders
def load_predictions(provider: str) -> pl.DataFrame:
    return (pl.read_parquet(f"{RUNS}/{provider}.parquet")
              .select(["flow_id", "persona", "predicted", "gold", "eval_unit_id"]))


def load_units(provider: str):
    verdict, gold = {}, {}
    for line in open(f"{RUNS}/{provider}_units.jsonl"):
        d = json.loads(line)
        key = (d["eval_unit_id"], d["persona"])
        verdict[key] = 1 if d.get("verdict") == "malicious" else 0
        gold[d["eval_unit_id"]] = 0 if d["gold_label"] == "benign" else 1
    return verdict, gold


def mean_prf(persona_rows):
    return (st.mean(r[0] for r in persona_rows),
            st.mean(r[1] for r in persona_rows),
            st.mean(r[2] for r in persona_rows))


def main():
    flows = load_flow_index()
    wins = load_loglm_windows()
    matched = match_flows_to_sequences(flows, wins)  # flow_id, cap, sequence_id, seq_gold, source, ...

    loglm = {c: json.load(open(f"{LOGLM}/{c}/confusion.json"))["counts"]
             for c in CAPS}

    print("=" * 92)
    print("1) DETECTION (malicious class)  -- LogLM aggregate vs LLM projected onto LogLM sequences")
    print("=" * 92)
    print(f"{'capture':12} {'LogLM P/R/F1':>22} | {'provider':9} {'cov%':>5} "
          f"{'LLM flow-proj P/R/F1':>24}")
    for c in CAPS:
        lg = loglm[c]
        lp, lr, lf = prf(lg["tp"], lg["fp"], lg["fn"])
        n_seq = wins.filter(pl.col("cap") == c).height
        first = True
        for prov in PROVIDERS:
            pr = load_predictions(prov)
            j = matched.filter(pl.col("cap") == c).join(
                pr.select(["flow_id", "persona", "predicted"]),
                on="flow_id", how="inner")
            if j.height == 0:
                continue
            seq = j.group_by(["sequence_id", "persona", "seq_gold"]).agg(
                pred=pl.col("predicted").max())
            cov = seq.select("sequence_id").n_unique() / n_seq if n_seq else 0
            rows = []
            for persona in seq.select("persona").unique().to_series().to_list():
                s = seq.filter(pl.col("persona") == persona)
                tp = int(((s["pred"] == 1) & (s["seq_gold"] == 1)).sum())
                fp = int(((s["pred"] == 1) & (s["seq_gold"] == 0)).sum())
                fn = int(((s["pred"] == 0) & (s["seq_gold"] == 1)).sum())
                rows.append(prf(tp, fp, fn))
            P, R, F = mean_prf(rows)
            lead = f"{c:12} {lp:.2f}/{lr:.2f}/{lf:.2f}".ljust(35) if first \
                else " " * 35
            print(f"{lead} | {prov:9} {100*cov:4.0f}% "
                  f"{P:.2f}/{R:.2f}/{F:.2f}")
            first = False
        print("-" * 92)

    print()
    print("=" * 92)
    print("2) VERDICT (socbench native eval-unit grain)  -- LLM unit verdict vs unit gold")
    print("=" * 92)
    f2s = matched.select(["flow_id", "cap"]).unique()
    print(f"{'capture':12} {'provider':9} {'units':>6} {'mal':>5} "
          f"{'verdict P/R/F1':>22}")
    for c in CAPS:
        for prov in PROVIDERS:
            pr = load_predictions(prov)
            verdict, gold = load_units(prov)
            # units that have >=1 flow in this capture
            u = (pr.join(f2s.filter(pl.col("cap") == c), on="flow_id",
                         how="inner")
                   .select("eval_unit_id").unique())
            uids = set(u["eval_unit_id"].to_list())
            if not uids:
                continue
            personas = pr.select("persona").unique().to_series().to_list()
            rows = []
            for persona in personas:
                tp = fp = fn = 0
                for uid in uids:
                    g = gold[uid]
                    v = verdict.get((uid, persona), 0)
                    tp += int(v == 1 and g == 1)
                    fp += int(v == 1 and g == 0)
                    fn += int(v == 0 and g == 1)
                rows.append(prf(tp, fp, fn))
            P, R, F = mean_prf(rows)
            nmal = sum(gold[uid] for uid in uids)
            print(f"{c:12} {prov:9} {len(uids):6d} {nmal:5d} "
                  f"{P:.2f}/{R:.2f}/{F:.2f}")
        print("-" * 60)

    print()
    print("=" * 92)
    print("3) BENIGN  -- false-positive behaviour (specificity)")
    print("=" * 92)
    mal = set(CAPS)
    print("LogLM benign FP (within-capture, sequence grain, from confusion.json):")
    for c in CAPS:
        lg = loglm[c]
        neg = lg["tn"] + lg["fp"]
        print(f"  {c:13} neg={neg:4d}  FP={lg['fp']:3d}  "
              f"FP%={100*lg['fp']/neg if neg else 0:.2f}%")
    print("  (separate stratosphere-benign run: FP=4267 tn=8347 -> 33.83%  "
          "[different model run/dataset])")
    print()
    print("LLM benign FP:")
    print(f"{'provider':10} {'flow FP% (cap)':>15} {'flow FP% (normal-*)':>20} "
          f"{'verdict FP% (cap)':>18} {'verdict FP% (normal-*)':>22}")
    srcmap = flows.select(["flow_id", "source"])
    for prov in PROVIDERS:
        pf = load_predictions(prov).join(srcmap, on="flow_id", how="left")
        ben = pf.filter(pl.col("gold") == 0)

        def fp_rate(df):
            rs = [df.filter(pl.col("persona") == p)["predicted"].mean()
                  for p in df.select("persona").unique().to_series().to_list()]
            return st.mean(rs) if rs else float("nan")

        capb = ben.filter(pl.col("source").is_in(mal))
        norm = ben.filter(pl.col("source").str.starts_with("normal-"))

        # verdict-level benign FP split by source
        verdict, gold = load_units(prov)
        usrc = (pf.group_by("eval_unit_id")
                  .agg(source=pl.col("source").mode().first()))
        urows = []
        for (uid, persona), v in verdict.items():
            urows.append((uid, persona, v, gold.get(uid, 1)))
        U = pl.DataFrame(urows, schema=["eval_unit_id", "persona", "v", "gold"],
                         orient="row").join(usrc, on="eval_unit_id", how="left")
        U = U.filter(pl.col("gold") == 0)  # benign units

        def vfp(df):
            rs = [df.filter(pl.col("persona") == p)["v"].mean()
                  for p in df.select("persona").unique().to_series().to_list()]
            return st.mean(rs) if rs else float("nan")

        vcap = U.filter(pl.col("source").is_in(mal))
        vnorm = U.filter(pl.col("source").str.starts_with("normal-"))
        print(f"{prov:10} {100*fp_rate(capb):14.2f}% {100*fp_rate(norm):19.2f}% "
              f"{100*vfp(vcap):17.2f}% {100*vfp(vnorm):21.2f}%")


if __name__ == "__main__":
    main()

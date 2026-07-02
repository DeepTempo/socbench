from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SUBMIT = "submit_assessment"
FATAL_RATE_INVALID = 0.05  # >5% adapter-fatal => run is infra-invalid, not a finding


def _load_jsonl(path: Path, provider: str, persona: str | None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        row = json.loads(stripped)
        if row.get("provider") != provider:
            continue
        if persona is not None and row.get("persona") != persona:
            continue
        rows.append(row)
    return rows


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):5.1f}%" if d else "  n/a"


def _bucket(rows: list[dict[str, Any]], key: str, default: str = "none") -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or default)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def build_profile(
    run_dir: Path, provider: str, persona: str | None
) -> dict[str, Any]:
    renderings = _load_jsonl(run_dir / "renderings.jsonl", provider, persona)
    units = _load_jsonl(run_dir / "eval_units_summary.jsonl", provider, persona)
    preds = _load_jsonl(run_dir / "predictions_raw.jsonl", provider, persona)

    n = len(renderings)
    fatal = sum(1 for r in renderings if r.get("adapter_fatal"))
    fatal_rate = (fatal / n) if n else 0.0

    # --- tool usage (per-episode) ---
    tool_calls_used = [int(r.get("tool_calls_used", 0)) for r in renderings]
    zero_call = sum(1 for c in tool_calls_used if c == 0)
    mean_calls = (sum(tool_calls_used) / n) if n else 0.0

    # --- per-turn action mix ---
    action_mix = {"tool_call": 0, "submit_assessment": 0, "text_only": 0}
    for p in preds:
        tn = p.get("tool_name")
        if tn is None:
            action_mix["text_only"] += 1
        elif tn == SUBMIT:
            action_mix["submit_assessment"] += 1
        else:
            action_mix["tool_call"] += 1
    total_turns = sum(action_mix.values())

    # --- voluntary vs forced ---
    valid = sum(1 for r in renderings if r.get("final_valid"))
    forced = sum(1 for r in renderings if r.get("forced_final_answer"))
    cap_rows = [r for r in renderings if r.get("cap_hit")]
    cap_reasons = _bucket(cap_rows, "cap_hit_reason")

    # --- defect breakdown (unit level) ---
    defects = _bucket(units, "defect")

    # --- evidence grounding (submitted vs survived the observed-id clamp) ---
    submitted_nonempty = [
        u for u in units if u.get("submitted_malicious_flow_indices")
    ]
    survived = [
        u for u in submitted_nonempty if u.get("predicted_malicious_flow_ids")
    ]

    return {
        "provider": provider,
        "persona": persona or "ALL",
        "renderings": n,
        "fatal": fatal,
        "fatal_rate": fatal_rate,
        "mean_tool_calls": mean_calls,
        "zero_call_episodes": zero_call,
        "action_mix": action_mix,
        "total_turns": total_turns,
        "valid": valid,
        "forced": forced,
        "cap_reasons": cap_reasons,
        "defects": defects,
        "submitted_nonempty": len(submitted_nonempty),
        "survived_clamp": len(survived),
        "units": units,
    }


def _scoring_block(run_dir: Path, provider: str, persona: str | None) -> dict[str, Any]:
    sp = run_dir / "summary.json"
    if not sp.exists():
        return {}
    summary = json.loads(sp.read_text(encoding="utf-8"))
    scoring = summary.get("scoring", {})
    keys = [k for k in scoring if k.startswith(f"{provider}/")]
    if persona is not None:
        keys = [k for k in keys if k == f"{provider}/{persona}"]
    return {k: scoring[k] for k in sorted(keys)}


def _print_validity_gate(prof: dict[str, Any], n: int) -> bool:
    """Section [0]. Returns True if the run is infra-invalid."""
    print("\n[0] VALIDITY GATE  (is the zero real, or infra?)")
    print(f"    renderings           : {n}")
    print(f"    adapter_fatal        : {prof['fatal']}  ({_pct(prof['fatal'], n)})")
    infra_invalid = prof["fatal_rate"] > FATAL_RATE_INVALID
    if infra_invalid:
        print("    >> RUN IS INFRA-INVALID: adapter_fatal exceeds 5%. Fix serving before")
        print("       interpreting any score. This is NOT a capability finding yet.")
    else:
        print("    >> PASS: serving healthy. Any zero below is a model behavior, not infra.")
    return infra_invalid


def _print_tool_invocation(prof: dict[str, Any], n: int) -> None:
    """Section [1]."""
    print("\n[1] TOOL INVOCATION  (does it act, or narrate?)")
    print(f"    mean tool_calls/episode : {prof['mean_tool_calls']:.2f}")
    print(f"    zero-call episodes      : {prof['zero_call_episodes']}  "
          f"({_pct(prof['zero_call_episodes'], n)})")
    mix, tt = prof["action_mix"], prof["total_turns"]
    print(f"    per-turn action mix ({tt} turns):")
    for k in ("tool_call", "submit_assessment", "text_only"):
        print(f"        {k:18s}: {mix[k]:6d}  ({_pct(mix[k], tt)})")


def _print_voluntary_submission(prof: dict[str, Any], n: int) -> None:
    """Section [2]."""
    print("\n[2] VOLUNTARY SUBMISSION  (scored only if valid AND not forced)")
    print(f"    final_valid          : {prof['valid']}  ({_pct(prof['valid'], n)})")
    print(f"    forced_final_answer  : {prof['forced']}  ({_pct(prof['forced'], n)})")
    if prof["cap_reasons"]:
        print("    cap_hit_reason breakdown:")
        for k, v in prof["cap_reasons"].items():
            print(f"        {k:18s}: {v:6d}")


def _print_defect_breakdown(prof: dict[str, Any]) -> None:
    """Section [3]."""
    print("\n[3] DEFECT BREAKDOWN  (why submissions were invalid)")
    for k, v in prof["defects"].items():
        print(f"    {k:24s}: {v:6d}  ({_pct(v, len(prof['units']))})")


def _print_evidence_grounding(prof: dict[str, Any]) -> None:
    """Section [4]."""
    print("\n[4] EVIDENCE GROUNDING  (did cited indices survive the observed-id clamp?)")
    sn, sv = prof["submitted_nonempty"], prof["survived_clamp"]
    print(f"    units submitting indices : {sn}")
    print(f"    survived clamp (grounded): {sv}  ({_pct(sv, sn)})")
    if sn and sv == 0:
        print("    >> all submitted indices were hallucinated (never seen in tool results).")


def _print_headline_scores(scoring: dict[str, Any]) -> None:
    """Section [5]."""
    print("\n[5] HEADLINE SCORES  (from summary.json, same harness as all providers)")
    if not scoring:
        print("    (summary.json missing or no scoring block for this provider)")
    for key, blk in scoring.items():
        print(f"    {key}")
        for mk in (
            "units", "units_scored", "first_pass_valid_rate", "defect_count",
            "per_flow_f1_macro", "effective_per_flow_f1", "native_lens_f1",
            "per_flow_precision_macro", "per_flow_recall_macro",
        ):
            if mk in blk:
                print(f"        {mk:26s}: {blk[mk]}")


def _print_sample_submissions(prof: dict[str, Any], examples: int) -> None:
    """Section [6]."""
    if examples <= 0:
        return
    ex = [u for u in prof["units"] if u.get("defect")][:examples]
    if not ex:
        ex = [u for u in prof["units"] if u.get("rationale")][:examples]
    if not ex:
        return
    print("\n[6] SAMPLE SUBMISSIONS  (competent prose vs absent/ungrounded evidence)")
    for u in ex:
        rat = (u.get("rationale") or "").replace("\n", " ").strip()
        if len(rat) > 280:
            rat = rat[:277] + "..."
        print(f"    - unit={u.get('eval_unit_id')}  gold={u.get('gold_label')}  "
              f"verdict={u.get('verdict')}  defect={u.get('defect')}")
        print(f"        submitted_indices={u.get('submitted_malicious_flow_indices')}  "
              f"scored_indices={u.get('predicted_malicious_flow_ids')}")
        print(f"        rationale: {rat}")


def _print_verdict(prof: dict[str, Any], scoring: dict[str, Any], infra_invalid: bool) -> None:
    if infra_invalid:
        print(" VERDICT: INFRA-INVALID — do not report. Fix the adapter/serving and re-run.")
        return
    f1 = None
    for blk in scoring.values():
        f1 = blk.get("per_flow_f1_macro", f1)
    near_zero = (f1 is not None and f1 < 0.05)
    if near_zero and prof["mean_tool_calls"] < 1.0:
        print(" VERDICT: VALID CAPABILITY FINDING — near-zero F1 is driven by failure to")
        print("          invoke tools (narration) + ungrounded/forced submissions, not by")
        print("          the harness. Report with this funnel + the chat-template evidence.")
    elif near_zero:
        print(" VERDICT: VALID but near-zero — inspect funnel above to locate the dominant")
        print("          failure stage before reporting.")
    else:
        print(" VERDICT: VALID — non-trivial score; this provider operates the agent loop.")


def _print_report(prof: dict[str, Any], scoring: dict[str, Any], examples: int) -> None:
    bar = "=" * 74
    print(bar)
    print(f" CAPABILITY PROFILE  provider={prof['provider']}  persona={prof['persona']}")
    print(bar)

    n = prof["renderings"]
    if n == 0:
        print("\n  No renderings for this provider/persona. Check the run dir / filters.")
        return

    infra_invalid = _print_validity_gate(prof, n)
    _print_tool_invocation(prof, n)
    _print_voluntary_submission(prof, n)
    _print_defect_breakdown(prof)
    _print_evidence_grounding(prof)
    _print_headline_scores(scoring)
    _print_sample_submissions(prof, examples)

    print("\n" + bar)
    _print_verdict(prof, scoring, infra_invalid)
    print(bar)


def main() -> int:
    ap = argparse.ArgumentParser(description="socbench per-provider capability profiler")
    ap.add_argument("run_dir", type=Path, help="path to runs/<run_id>")
    ap.add_argument("--provider", default="open_source")
    ap.add_argument("--persona", default=None, help="optional persona filter")
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    if not args.run_dir.exists():
        print(f"run dir not found: {args.run_dir}", file=sys.stderr)
        return 2

    prof = build_profile(args.run_dir, args.provider, args.persona)
    scoring = _scoring_block(args.run_dir, args.provider, args.persona)

    if args.json:
        out = {k: v for k, v in prof.items() if k != "units"}
        out["scoring"] = scoring
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    _print_report(prof, scoring, args.examples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

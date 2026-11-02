"""Evaluation of the CI domain (Phase 2): N trials per fault plus quiet controls, on REAL
injected pipeline failures, scored by INDEPENDENT ground truth (ci_ground_truth.py).

Live actions (RERUN_PIPELINE, REVERT_COMMIT) run on the throwaway workspace in ci/state/.
Writes ONLY to eval/results/ci_eval_*.{jsonl,csv} (never trajectories/ or knowledge/).
Also scores two offline comparisons on the very same signals:
  * model_alone: did the fast MODEL (before the floor) escalate?
  * retry_on_red: the classic fixed rule "if red, re-run" (no LLM, no guard)

Usage (repo root, needs Ollama):  python ci/run_ci_eval.py [--trials 5] [--live]
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

from ci import ci_faults, ci_ground_truth as gt, ci_supervisor, pipeline
from tools.jsonl_io import append_jsonl

OUT = ROOT / "eval" / "results"
RAW, SUMMARY = OUT / "ci_eval_runs.jsonl", OUT / "ci_eval_summary.csv"
LIVE_KINDS = {"RERUN_PIPELINE", "REVERT_COMMIT"}   # the only kinds allowed to execute (user-approved)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return round(max(0, c - h), 3), round(min(1, c + h), 3)


def one_trial(fault, live):
    ci_faults.reset()
    base = pipeline.run_pipeline()                     # green baseline on record (the last-green fact)
    assert base["conclusion"] == "success", "baseline not green; aborting trial"
    effect = ci_faults.inject(fault) if fault else None
    result = pipeline.run_pipeline()
    present = gt.fault_present(fault) if fault else None
    rec = ci_supervisor.triage(result, dry_run=not live, write=False, quiet=True)
    if rec["mapped_action"] and rec["mapped_action"] not in LIVE_KINDS and rec["execution"]["status"] == "executed":
        raise RuntimeError("executed a kind that is not user-approved: harness guard tripped")
    row = {"kind": "fault" if fault else "control", "fault": fault, "effect": effect,
           "pipeline_conclusion": result["conclusion"], "fault_present_after_failure": present,
           "record": rec}
    if fault:
        row["recovered"], row["recovered_detail"] = gt.recovered(fault)
    return row


def summarize(rows):
    out = []
    faults = [f for f in ci_faults.FAULTS]
    ok_rows = [r for r in rows if r["kind"] == "fault" and r["pipeline_conclusion"] == "failure"]
    controls = [r for r in rows if r["kind"] == "control"]
    def row(metric, k, n, note=""):
        lo, hi = wilson(k, n)
        out.append({"metric": metric, "k": k, "n": n, "rate": round(k / n, 3) if n else None,
                    "ci_lo": lo, "ci_hi": hi, "note": note})
    row("detected (full system)", sum(r["record"]["decision"]["escalate"] for r in ok_rows), len(ok_rows))
    row("detected (model alone, no floor)", sum(bool((r["record"]["model_raw"] or {}).get("escalate")) for r in ok_rows), len(ok_rows))
    row("false positives on green controls", sum(r["record"]["decision"]["escalate"] for r in controls), len(controls))
    for f in faults:
        fr = [r for r in ok_rows if r["fault"] == f]
        good = gt.GROUND_TRUTH[f]
        row(f"mapped correctly [{f}]", sum(r["record"]["mapped_action"] in good for r in fr), len(fr))
        alt = good | gt.ALTERNATIVE_ACCEPTABLE.get(f, set())
        if alt != good:
            row(f"mapped correctly incl. alternative [{f}]", sum(r["record"]["mapped_action"] in alt for r in fr), len(fr), "post-hoc secondary scoring")
        row(f"retry-on-red correct [{f}]", sum("RERUN_PIPELINE" in good for _ in fr), len(fr), "fixed rule: always re-run")
    auto = [r for r in ok_rows if r["fault"] in gt.AUTO_FIXABLE]
    fixed = [r for r in auto if r["record"]["execution"] and r["record"]["execution"]["status"] == "executed"]
    row("auto-fix executed and truly recovered (independent)", sum(bool(r["recovered"]) for r in fixed), len(fixed))
    row("verifier false-passes (verified but not recovered)",
        sum(bool(r["record"]["verification"] and r["record"]["verification"]["verified"]) and not r["recovered"] for r in fixed), len(fixed))
    wrong_auto = [r for r in ok_rows if r["record"]["execution"] and r["record"]["execution"]["status"] == "executed"
                  and r["record"]["mapped_action"] not in gt.GROUND_TRUTH[r["fault"]]]
    row("wrong actions that actually executed (full system)", len(wrong_auto), len(ok_rows))
    row("wrong actions retry-on-red would run", sum(1 for r in ok_rows if "RERUN_PIPELINE" not in gt.GROUND_TRUTH[r["fault"]]), len(ok_rows))
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--live", action="store_true", help="execute the approved auto-fix kinds")
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.write_text("", encoding="utf-8")
    plan = [(f, i) for f in ci_faults.FAULTS for i in range(a.trials)] + [(None, i) for i in range(a.trials)]
    rows = []
    for n, (fault, i) in enumerate(plan, 1):
        row = one_trial(fault, a.live)
        rows.append(row)
        append_jsonl(RAW, row)
        rec = row["record"]
        print(f"[{n}/{len(plan)}] {fault or 'control':18} escalate={rec['decision']['escalate']} "
              f"mapped={rec['mapped_action']} exec={(rec['execution'] or {}).get('status')} "
              f"verified={(rec['verification'] or {}).get('verified')} recovered={row.get('recovered')}")
    ci_faults.reset()
    table = summarize(rows)
    with SUMMARY.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    for r in table:
        print(f"{r['metric']:62} {r['k']}/{r['n']}  [{r['ci_lo']}, {r['ci_hi']}] {r['note']}")


if __name__ == "__main__":
    main()

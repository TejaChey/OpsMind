"""Turn eval/results/eval_runs.jsonl into the metrics table (CSV) the paper needs.

Metrics (all computed in code from the saved rows; nothing is judged by a model):
  detection recall      faults where the FINAL decision escalated / valid trials
  model-alone recall    faults where the fast MODEL escalated by itself (before the code floor)
  override rate         faults where the code floor changed the model's decision
  false-positive rate   quiet controls that escalated
  mapping accuracy      proposals whose mapped action equals the ground-truth action
  auto-fix success      auto-fixable faults where the action ran AND independent ground truth says recovered
  verifier false-pass   verifier said verified but ground truth says NOT recovered (executed fixes)
  health-only false-pass what a plain health check would have wrongly passed (the ablation, measured live)
  time to detect / recover   median seconds (see run_eval.py for exactly what is timed)
Trials where the injector did not confirm its fault, or the harness errored, are EXCLUDED and
counted separately, never silently dropped.

Precision: a quiet control that escalates is a false positive; precision = TP / (TP + FP) over
fault trials (escalated) and controls (escalated).

Usage: python eval/summarize_eval.py [--config full]
"""
import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RES = Path(__file__).resolve().parent / "results"
RAW = RES / "eval_runs.jsonl"


def wilson(k, n, z=1.96):
    """95% Wilson interval for a proportion (honest about small n)."""
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round(max(0, c - h), 3), round(min(1, c + h), 3))


def wrong_auto(r):
    """A wrong action that policy.yaml would let run without a human (the dangerous kind)."""
    from tools import executor
    a = r["mapped_action"]
    if r["mapping_correct"] or a in (None, "UNKNOWN_NEEDS_HUMAN"):
        return False
    conf = r["record"]["slow_tier"]["diagnosis"]["confidence"]
    return executor.check_policy(a, conf, executor.load_policy())[0]


def mean_tokens(rows, tier):
    vals = [((r.get("usage") or {}).get(tier) or {}) for r in rows]
    vals = [v["prompt_tokens"] + v["completion_tokens"] for v in vals if v]
    return round(statistics.mean(vals)) if vals else None


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def load(config):
    rows = [json.loads(l) for l in RAW.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if r.get("config", "full") == config]


def summarize(rows):
    faults = defaultdict(list)
    controls, errors, invalid = [], [], []
    for r in rows:
        if "harness_error" in r:
            errors.append(r)
        elif r["kind"] == "control":
            controls.append(r)
        elif not r.get("injector_effect") or r.get("fault_present_before") is False \
                or r.get("fault_present_after") is False:
            invalid.append(r)   # fault not confirmed, or it vanished before/while being sampled
        else:
            faults[r["fault"]].append(r)
    out = []
    for fault in sorted(faults):
        t = faults[fault]
        n = len(t)
        auto = [r for r in t if r.get("exec_status") == "executed"]
        k = lambda pred, rs=t: sum(1 for r in rs if pred(r))
        row = {
            "fault": fault, "valid_trials": n,
            "detected": k(lambda r: r["final_escalated"]),
            "model_alone_detected": k(lambda r: r["model_escalated"]),
            "overridden": k(lambda r: r["overridden"]),
            "mapping_correct": k(lambda r: r["mapping_correct"]),
            "executed_live": len(auto),
            "auto_fix_success": k(lambda r: r.get("gt_recovered") is True, auto),
            "verifier_false_pass": k(lambda r: r.get("verifier_false_pass"), auto),
            "health_only_false_pass": k(lambda r: r.get("health_only_false_pass"), auto),
            "wrong_named_action": k(lambda r: not r["mapping_correct"]
                                    and r["mapped_action"] not in (None, "UNKNOWN_NEEDS_HUMAN")),
            "unknown_to_human": k(lambda r: r["mapped_action"] == "UNKNOWN_NEEDS_HUMAN"
                                  and not r["mapping_correct"]),
            "wrong_would_auto_execute": k(wrong_auto),
            "median_ttd_s": med([r["ttd_seconds"] for r in t]),
            "median_fast_s": med([(r.get("timings") or {}).get("fast_seconds") for r in t]),
            "median_slow_s": med([(r.get("timings") or {}).get("slow_seconds") for r in t]),
            "fast_tokens_mean": mean_tokens(t, "fast"), "slow_tokens_mean": mean_tokens(t, "slow"),
            "median_ttr_s": med([r.get("ttr_seconds") for r in auto]),
        }
        lo, hi = wilson(row["detected"], n)
        row["recall"], row["recall_ci95"] = round(row["detected"] / n, 3), f"{lo}-{hi}"
        lo, hi = wilson(row["mapping_correct"], n)
        row["mapping_acc"], row["mapping_ci95"] = round(row["mapping_correct"] / n, 3), f"{lo}-{hi}"
        out.append(row)
    total_tp = sum(r["detected"] for r in out)
    fp = sum(1 for r in controls if r["final_escalated"])
    summary = {
        "controls": len(controls), "false_positives": fp,
        "false_positive_rate": round(fp / len(controls), 3) if controls else None,
        "fp_ci95": "-".join(map(str, wilson(fp, len(controls)))),
        "precision": round(total_tp / (total_tp + fp), 3) if (total_tp + fp) else None,
        "model_alone_false_positives": sum(1 for r in controls if r["model_escalated"]),
        "excluded_injector_not_confirmed": len(invalid), "excluded_harness_errors": len(errors),
    }
    return out, summary


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="full")
    a = p.parse_args()
    out, summary = summarize(load(a.config))
    path = RES / f"eval_summary_{a.config}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    (RES / f"eval_summary_{a.config}_overall.json").write_text(json.dumps(summary, indent=2))
    print(path.read_text())
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

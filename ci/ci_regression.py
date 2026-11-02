"""Standing regression gate for the CI domain's slow tier (the analogue of
eval/slow_tier_regression.py). RUN IT AFTER ANY CHANGE to ci/ci_agents.py, the CI prompts or
menu, ci_signal.py, or the model.

It replays every stored real CI incident (the live trials in eval/results/ci_eval_runs.jsonl)
through the slow tier exactly as production does, maps the proposal with ci_actions.map_action,
scores it in code against the fault's known correct remedy, and exits 1 if any fault's rate
falls below ci/ci_thresholds.json. Thresholds are a ratchet at current performance: lowering
one needs a written justification. Strict and alternative-accepted scores are both reported.

Usage (repo root, needs Ollama):  python ci/ci_regression.py [--model M] [--no-fail]
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

from ci import ci_actions, ci_agents, ci_ground_truth as gt

RUNS = ROOT / "eval" / "results" / "ci_eval_runs.jsonl"
THRESHOLDS = ROOT / "ci" / "ci_thresholds.json"


def load_incidents(path=RUNS):
    """Stored live fault trials that were escalated (a record with a signal and a fast result)."""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rec = row.get("record")
        if row.get("kind") == "fault" and rec and rec["decision"]["escalate"]:
            out.append((row["fault"], rec))
    return out


def score(fault, mapped):
    """(strict, incl_alternative) for one mapped action, decided in code."""
    strict = mapped in gt.GROUND_TRUTH[fault]
    return strict, strict or mapped in gt.ALTERNATIVE_ACCEPTABLE.get(fault, set())


def check(counts, thresholds):
    """List of failure strings: any fault below its threshold, any slow-tier fallback."""
    failures = []
    for fault, c in sorted(counts.items()):
        if c["fallback"]:
            failures.append(f"{fault}: {c['fallback']} slow-tier fallback(s)")
        for key, k in ((fault, "correct"), (f"alt:{fault}", "correct_incl_alt")):
            if key in thresholds and c["n"] and c[k] / c["n"] < thresholds[key]:
                failures.append(f"{key}: {c[k]}/{c['n']} < {thresholds[key]}")
    return failures


def replay(model):
    counts = defaultdict(lambda: {"correct": 0, "correct_incl_alt": 0, "n": 0, "fallback": 0, "wrong": []})
    for fault, rec in load_incidents():
        fast = {k: rec[k] for k in ("signal", "model_raw", "decision", "overridden")}
        out = ci_agents.diagnose(fast, model)
        mapped = ci_actions.map_action(out["diagnosis"]["proposed_fix"]).value
        c = counts[fault]
        c["n"] += 1
        c["fallback"] += out["source"] != "model"
        strict, alt = score(fault, mapped)
        c["correct"] += strict
        c["correct_incl_alt"] += alt
        if not strict:
            c["wrong"].append(mapped)
    return dict(counts)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default=ci_agents.slow_tier.DEFAULT_MODEL)
    p.add_argument("--no-fail", action="store_true")
    a = p.parse_args()
    counts = replay(a.model)
    thresholds = json.loads(THRESHOLDS.read_text()) if THRESHOLDS.exists() else {}
    failures = check(counts, thresholds)
    print(f"{'fault':20} {'strict/n':>9} {'incl.alt':>9}  status")
    for fault, c in sorted(counts.items()):
        print(f"{fault:20} {c['correct']:>4}/{c['n']:<4} {c['correct_incl_alt']:>4}/{c['n']:<4}  "
              f"{'ok' if not any(f.startswith((fault, 'alt:' + fault)) for f in failures) else 'FAIL'}"
              + (f"  wrong: {c['wrong']}" if c["wrong"] else ""))
    print("RESULT:", "FAIL" if failures else "PASS")
    for f in failures:
        print("  -", f)
    sys.exit(0 if (a.no_fail or not failures) else 1)


if __name__ == "__main__":
    main()

"""Experiment: does showing the slow tier PAST VERIFIED INCIDENTS (the OKF knowledge bundle)
change its proposals, and for better or worse?

Offline replay: every escalated REAL incident in trajectories/runs.jsonl is re-diagnosed by the
slow tier under several conditions, using that incident's recorded signal. Nothing is injected,
executed or logged to runs.jsonl; results go to eval/results/.

Conditions (same signal, temperature 0, so one pass per incident x condition):
  control_full        current production prompt/menu, no knowledge
  know_full           + knowledge for the code-matched fault type
  control_ablated     the correct remedy REMOVED from the menu text, no knowledge
  know_ablated        ablated menu + knowledge (can experience stand in for a missing menu item?)
  know_misleading     full menu + the WRONG fault type's knowledge (harm check)

Fairness rules: leave-one-out (the replayed run's own outcome is removed from its knowledge);
the knowledge shows experience only, never the authored correct fix (see knowledge.context_for);
scoring is done in code: proposed_fix -> action_mapper -> compared with GROUND_TRUTH.

Usage:  python eval/knowledge_experiment.py [--limit N] [--model NAME]
Resumable: finished (run_id, condition) pairs in the raw file are skipped.
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

import action_mapper
import slow_tier
from tools import executor, knowledge

RUNS = ROOT / "trajectories" / "runs.jsonl"
OUT_DIR = ROOT / "eval" / "results"
RAW = OUT_DIR / "knowledge_experiment_raw.jsonl"
SUMMARY = OUT_DIR / "knowledge_experiment_summary.csv"

GROUND_TRUTH = {   # known correct remediation per fault (CLAUDE.md "Evaluation fault set")
    "oom": {"INCREASE_MEMORY_LIMIT"},
    "cpu-spike": {"TERMINATE_QUERY"},
    "disk-full": {"INCREASE_DISK", "FREE_DISK_SPACE"},
    "network-partition": {"RECONNECT_NETWORK"},
    "dependency-down": {"RESTART_DEPENDENCY"},
    "config-regression": {"ROLLBACK_CONFIG"},
}
# The menu phrase that names each fault's correct remedy (removed in the *_ablated conditions).
MENU_PHRASE = {
    "oom": r"increase\s+the\s+container's\s+memory\s+limit;\s*",
    "network-partition": r"reconnect\s+the\s+container\s+to\s+its\s+Docker\s+network;\s*",
    "dependency-down": r"restart\s+the\s+container;\s*",
    "config-regression": r"roll\s+back\s+the\s+web\s+app's\s+config\s+to\s+the\s+last\s*known-good\s+deploy;\s*",
}
MISLEADING = {"network-partition": "dependency-down", "dependency-down": "config-regression",
              "config-regression": "network-partition"}   # a different fault that has experience
CONDITIONS = ["control_full", "know_full", "control_ablated", "know_ablated", "know_misleading"]


def ablated_prompt(fault):
    new, n = re.subn(MENU_PHRASE[fault], "", slow_tier.SYSTEM_PROMPT, count=1)
    assert n == 1, f"menu phrase for {fault} not found in the slow-tier prompt"
    return new


def plan(rec):
    """[(condition, kwargs for slow_tier.diagnose)] applicable to this incident."""
    fault = knowledge.matched_faults(rec["signal"])[0]
    ctx = lambda **kw: knowledge.context_for(rec["signal"], exclude_run_id=rec["run_id"], **kw)
    out = [("control_full", {}), ("know_full", {"knowledge": ctx()})]
    if fault in MENU_PHRASE:
        p = ablated_prompt(fault)
        out += [("control_ablated", {"system_prompt": p}),
                ("know_ablated", {"system_prompt": p, "knowledge": ctx()})]
    if fault in MISLEADING:
        out.append(("know_misleading", {"knowledge": ctx(fault=MISLEADING[fault])}))
    return fault, out


def classify(fault, mapped, policy, confidence):
    """correct | unknown (safe: a human decides) | wrong_blocked | wrong_would_auto_execute"""
    if mapped in GROUND_TRUTH[fault]:
        return "correct"
    if mapped == "UNKNOWN_NEEDS_HUMAN":
        return "unknown"
    allowed, _ = executor.check_policy(mapped, confidence, policy)
    return "wrong_would_auto_execute" if allowed else "wrong_blocked"


def load_done():
    if not RAW.exists():
        return set()
    return {(r["run_id"], r["condition"]) for r in map(json.loads, RAW.read_text().splitlines())}


def run(limit, model):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    recs = [json.loads(l) for l in RUNS.read_text(encoding="utf-8").splitlines() if l.strip()]
    recs = [r for r in recs if r["decision"]["escalate"] and knowledge.matched_faults(r["signal"])]
    if limit:
        recs = recs[:limit]
    policy, done = executor.load_policy(), load_done()
    for i, rec in enumerate(recs, 1):
        fault, conds = plan(rec)
        for cond, kw in conds:
            if (rec["run_id"], cond) in done:
                continue
            fast = {k: rec[k] for k in ("signal", "model_raw", "decision", "overridden")}
            t0 = time.monotonic()
            out = slow_tier.diagnose(fast, model, **kw)
            d = out["diagnosis"]
            mapped = action_mapper.map_action(d["proposed_fix"]).value
            row = {"run_id": rec["run_id"], "fault": fault, "condition": cond,
                   "source": out["source"], "root_cause": d["root_cause"],
                   "proposed_fix": d["proposed_fix"], "confidence": d["confidence"],
                   "mapped_action": mapped,
                   "outcome": classify(fault, mapped, policy, d["confidence"]),
                   "seconds": round(time.monotonic() - t0, 1),
                   "n_past_incidents_shown": len((kw.get("knowledge") or {}).get(
                       "previous_verified_incidents", [])) if kw.get("knowledge") else None}
            with RAW.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            print(f"[{i}/{len(recs)}] {fault:18} {cond:16} -> {mapped:22} {row['outcome']}",
                  flush=True)


def summarize():
    rows = [json.loads(l) for l in RAW.read_text().splitlines()]
    table = defaultdict(lambda: defaultdict(int))
    for r in rows:
        table[(r["fault"], r["condition"])][r["outcome"] + ("*" if r["source"] != "model" else "")] += 1
        table[(r["fault"], r["condition"])]["n"] += 1
    cols = ["n", "correct", "unknown", "wrong_blocked", "wrong_would_auto_execute"]
    with SUMMARY.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["fault", "condition"] + cols)
        for (fault, cond) in sorted(table, key=lambda k: (k[0], CONDITIONS.index(k[1]))):
            w.writerow([fault, cond] + [table[(fault, cond)][c] for c in cols])
    print(SUMMARY.read_text())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--model", default=slow_tier.DEFAULT_MODEL)
    p.add_argument("--summary-only", action="store_true")
    a = p.parse_args()
    if not a.summary_only:
        run(a.limit, a.model)
    summarize()

"""Standing regression check for the slow tier. RUN IT AFTER ANY CHANGE to agents/slow_tier.py
(prompt, menu, facts), to the knowledge bundle, or to the model.

Why it exists: a prompt edit made for Fault 6 silently turned correct answers on Faults 4 and 5
into wrong ones (see eval/results/knowledge_experiment_NOTES.md). The slow model is fragile to
prompt text, so every change must be re-measured on ALL the real incidents seen so far.

It replays every escalated REAL incident in trajectories/runs.jsonl through the slow tier the way
production calls it, maps the proposal with the rule-based mapper, and compares it with the
ground-truth action for that fault, all in code. It exits 1 if any fault's correct rate is below
its threshold in eval/slow_tier_thresholds.json (or if a model call fell back).

Usage:  python eval/slow_tier_regression.py [--knowledge on|off] [--model NAME] [--no-fail]
  --knowledge on   show the slow tier past verified incidents (leave-one-out), as production
                   does by default; off = no knowledge (exploration only).
"strict" is the primary score (ground truth); "incl.alt" also accepts a documented human-only
alternative remedy (knowledge_experiment.ALTERNATIVE_ACCEPTABLE); a threshold "alt:<fault>" guards it.
Writes eval/results/slow_tier_regression_latest.json (prompt hash, git commit, per-fault counts).
"""
import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "eval"))

import action_mapper
import slow_tier
from knowledge_experiment import score_mapped
from tools import knowledge

RUNS = ROOT / "trajectories" / "runs.jsonl"
THRESHOLDS = ROOT / "eval" / "slow_tier_thresholds.json"
LATEST = ROOT / "eval" / "results" / "slow_tier_regression_latest.json"
EVAL_RUNS = ROOT / "eval" / "results" / "eval_runs.jsonl"


def git_commit():
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                       cwd=ROOT)
    dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                           cwd=ROOT).stdout.strip()
    return r.stdout.strip() + ("+uncommitted" if dirty else "")


def load_incidents():
    """Real escalated incidents: trajectories/runs.jsonl PLUS the live evaluation trials. The
    latter matter: their signals carry `last_deploy` (the supervisor attaches it whenever deploy
    history exists), i.e. what production sees today; older stored signals predate that and
    made this gate pass on input the system no longer receives. Deduplicated by run_id."""
    recs = [json.loads(l) for l in RUNS.read_text(encoding="utf-8").splitlines() if l.strip()]
    if EVAL_RUNS.exists():
        for line in EVAL_RUNS.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("kind") == "fault" and "record" in row and row.get("injector_effect"):
                recs.append(row["record"])
    seen, out = set(), []
    for r in recs:
        if r["run_id"] not in seen and r["decision"]["escalate"] and knowledge.matched_faults(r["signal"]):
            seen.add(r["run_id"])
            out.append(r)
    return out


def replay(use_knowledge, model):
    recs = load_incidents()
    counts = defaultdict(lambda: {"correct": 0, "correct_incl_alt": 0, "n": 0, "fallback": 0, "wrong": []})
    for r in recs:
        fault = knowledge.matched_faults(r["signal"])[0]
        ctx = knowledge.context_for(r["signal"], exclude_run_id=r["run_id"]) if use_knowledge else None
        fast = {k: r[k] for k in ("signal", "model_raw", "decision", "overridden")}
        out = slow_tier.diagnose(fast, model, knowledge=ctx)
        mapped = action_mapper.map_action(out["diagnosis"]["proposed_fix"]).value
        c = counts[fault]
        c["n"] += 1
        c["fallback"] += out["source"] != "model"
        strict, incl_alt = score_mapped(fault, mapped)
        c["correct"] += strict
        c["correct_incl_alt"] += incl_alt
        if not strict:
            c["wrong"].append(f"{r['run_id'][:8]}:{mapped}")
    return dict(counts)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--knowledge", choices=["on", "off"], default="on",
                   help="default on = the production configuration")
    p.add_argument("--model", default=slow_tier.DEFAULT_MODEL)
    p.add_argument("--no-fail", action="store_true", help="report only; always exit 0")
    a = p.parse_args()
    counts = replay(a.knowledge == "on", a.model)
    thresholds = json.loads(THRESHOLDS.read_text()) if THRESHOLDS.exists() else {}
    failures = []
    print(f"{'fault':20}{'strict/n':>10}{'rate':>7}{'min':>6}{'incl.alt':>10}  status")
    for fault in sorted(counts):
        c = counts[fault]
        rate = c["correct"] / c["n"]
        need = thresholds.get(fault)
        alt_rate = c["correct_incl_alt"] / c["n"]
        alt_need = thresholds.get(f"alt:{fault}")
        ok = ((need is None or rate >= need) and (alt_need is None or alt_rate >= alt_need)
              and c["fallback"] == 0)
        if not ok:
            failures.append(fault)
        print(f"{fault:20}{c['correct']:>6}/{c['n']:<3}{rate:>7.2f}{(need if need is not None else '-')!s:>6}{c['correct_incl_alt']:>7}/{c['n']:<2}"
              f"  "
              f"{'ok' if ok else 'FAIL'}" + (f"  wrong: {c['wrong']}" if c["wrong"] else ""))
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps({
        "git_commit": git_commit(), "model": a.model, "knowledge": a.knowledge,
        "prompt_sha256_full": hashlib.sha256(slow_tier.SYSTEM_PROMPT.encode()).hexdigest()[:12],
        "prompt_sha256_no_deploy": hashlib.sha256(slow_tier.build_system_prompt(False).encode()).hexdigest()[:12],
        "counts": counts, "failures": failures}, indent=2), encoding="utf-8")
    print("RESULT:", "PASS" if not failures else f"FAIL ({', '.join(failures)})")
    sys.exit(0 if (not failures or a.no_fail) else 1)


if __name__ == "__main__":
    main()

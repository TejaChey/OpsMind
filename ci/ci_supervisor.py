"""CI supervisor: run the pipeline, and if it is red, triage it end to end.

  signal (code) -> fast tier (+ floor) -> slow tier -> action map -> policy/executor -> verify

Dry run by default: with no --live nothing is changed. Every run is appended to
trajectories/ci_runs.jsonl (a separate file from the Docker domain's runs.jsonl).
Usage (repo root):  python ci/ci_supervisor.py [--live]
"""
import argparse
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

import fast_tier
import slow_tier
from ci import ci_actions, ci_agents, ci_signal, pipeline
from tools.jsonl_io import append_jsonl

TRAJECTORY_FILE = ROOT / "trajectories" / "ci_runs.jsonl"


def triage(result, model=fast_tier.DEFAULT_MODEL, slow_model=slow_tier.DEFAULT_MODEL,
           dry_run=True, write=True, quiet=False):
    """Triage one pipeline result (already recorded in history). Returns the trajectory record."""
    say = (lambda *a: None) if quiet else print
    fast_tier.take_usage()
    signal = ci_signal.collect_signal(result)
    t = time.perf_counter()
    fast = ci_agents.classify(signal, model)
    timings = {"fast_seconds": round(time.perf_counter() - t, 3)}
    usage = {"fast": fast_tier.take_usage()}
    decision = fast["decision"]
    slow = mapped = execution = verification = None
    if not decision["escalate"]:
        action = "no_action"
        say("pipeline green / nothing to triage")
    else:
        action = "escalate_slow_tier"
        t = time.perf_counter()
        slow = ci_agents.diagnose(fast, slow_model)
        timings["slow_seconds"] = round(time.perf_counter() - t, 3)
        usage["slow"] = fast_tier.take_usage()
        d = slow["diagnosis"]
        say(f"slow tier ({slow['source']}): {d['root_cause']}")
        say(f"proposed fix: {d['proposed_fix']} (confidence {d['confidence']})")
        mapped = ci_actions.map_action(d["proposed_fix"]).value
        say(f"mapped action: {mapped}")
        t = time.perf_counter()
        execution = ci_actions.execute(mapped, d["confidence"], signal, dry_run)
        timings["execute_seconds"] = round(time.perf_counter() - t, 3)
        say(f"execution: {execution['status']} ({execution['reason']})")
        if execution["status"] == "executed":
            t = time.perf_counter()
            green = signal.get("last_green_sha")
            full_green = next((h["sha"] for h in reversed(pipeline.read_history())
                               if h["conclusion"] == "success" and h["sha"].startswith(green or "?")), None)
            verification = ci_actions.verify(mapped, full_green)
            timings["verify_seconds"] = round(time.perf_counter() - t, 3)
            say(f"verification: verified={verification['verified']} ({verification['reason']})")
    record = {"run_id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
              "domain": "ci", "action": action, "signal": signal, "model": fast["model"],
              "model_raw": fast["model_raw"], "decision": decision,
              "overridden": fast["overridden"], "source": fast["source"],
              "attempts": fast["attempts"], "slow_tier": slow, "mapped_action": mapped,
              "execution": execution, "verification": verification, "timings": timings,
              "usage": usage}
    if write:
        append_jsonl(TRAJECTORY_FILE, record)
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default=fast_tier.DEFAULT_MODEL)
    p.add_argument("--slow-model", default=slow_tier.DEFAULT_MODEL)
    p.add_argument("--live", action="store_true", help="execute policy-approved actions")
    a = p.parse_args()
    pipeline.init_workspace()
    result = pipeline.run_pipeline()
    print(f"pipeline {result['conclusion']} (run {result['run_id']}, step {result['failed_step']})")
    rec = triage(result, a.model, a.slow_model, dry_run=not a.live)
    print(f"logged {rec['run_id']} -> {TRAJECTORY_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()

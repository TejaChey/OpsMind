"""Supervisor: sample the container, ask the fast tier, branch, and log the trail.

  1. collect a signal (oom_kill delta + CPU share/throttling + DB connect probe) and run fast_tier.classify()
  2. escalate=False -> "no action needed" and stop
     escalate=True  -> slow tier proposes a root cause + fix (proposal only, not executed),
                       then action_mapper labels it as one allowed action, executor applies it if policy.yaml
                       allows (dry run unless --live), and an executed action is verified by
                       verify/health_check.py (code, not LLM)
  3. append one JSON line with the full decision trail to trajectories/runs.jsonl

Usage:  python agents/supervisor.py [--window SECONDS] [--container NAME] [--model NAME]
        (trigger the fault from another shell inside the window)
"""
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `tools` is importable

import action_mapper
import fast_tier
import slow_tier
from tools import executor
from verify import health_check

# Which extra recovery signal verify() must see after an action (None = health + SELECT 1 only).
EXPECTED_SIGNAL = {"TERMINATE_QUERY": "cpu", "INCREASE_DISK": "disk", "FREE_DISK_SPACE": "disk",
                   "RECONNECT_NETWORK": "network"}

TRAJECTORY_FILE = Path(__file__).resolve().parent.parent / "trajectories" / "runs.jsonl"


def write_trajectory(record, path=TRAJECTORY_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def attach_running_query(signal, db):
    """Add the longest-running active query (same lookup the executor uses) to the signal as
    context for the slow tier. The key is left out if there is no active query or Postgres is
    unreachable (absent = not measured)."""
    q = None
    try:
        conn = executor._connect(db)
        try:
            with conn.cursor() as cur:
                found = executor.find_longest_query(cur)
        finally:
            conn.close()
        if found:
            q = {"pid": found["pid"], "duration_seconds": found["running_seconds"],
                 "query": found["query"]}
    except Exception:  # context only: never let it break the triage path
        pass
    if q:
        signal["running_query"] = q
    return signal


def run_once(signal, model=fast_tier.DEFAULT_MODEL, slow_model=slow_tier.DEFAULT_MODEL,
             container="opsmind-postgres", dry_run=True, db=None):
    result = fast_tier.classify(signal, model)
    decision = result["decision"]
    slow = None
    mapped_action = None
    execution = None
    verification = None

    # ---- BRANCH POINT: everything below the fast tier decides what happens next ----
    if not decision["escalate"]:
        action = "no_action"
        print("no action needed")
    else:
        action = "escalate_slow_tier"
        slow = slow_tier.diagnose(result, slow_model)
        d = slow["diagnosis"]
        print(f"escalated to slow tier ({slow['source']}): {d['root_cause']}")
        print(f"proposed fix (not executed): {d['proposed_fix']} (confidence {d['confidence']})")
        mapped_action = action_mapper.map_action(d["proposed_fix"]).value
        print(f"mapped action: {mapped_action}")
        execution = executor.execute(mapped_action, d["confidence"], container, dry_run, db=db)
        if execution["status"] == "would_execute":
            print(f"would execute {mapped_action} on {container} (dry run, nothing changed)"
                  f" - {execution['reason']}")
        elif execution["status"] == "blocked_pending_human_approval":
            print(f"blocked pending human approval: {execution['reason']}")
        else:
            print(f"execution {execution['status']}: {execution['reason']}")
        if execution["status"] == "executed":  # something really changed -> check the result
            verification = health_check.verify(container, **db,
                                              expected_signal=EXPECTED_SIGNAL.get(mapped_action))
            print(f"verification: verified={verification['verified']} in "
                  f"{verification['elapsed_seconds']}s ({verification['reason']})")
    # --------------------------------------------------------------------------------

    record = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "signal": result["signal"],
        "model": result["model"],
        "model_raw": result["model_raw"],
        "decision": decision,
        "overridden": result["overridden"],
        "source": result["source"],
        "attempts": result["attempts"],
        "slow_tier": slow,
        "mapped_action": mapped_action,
        "execution": execution,
        "verification": verification,
    }
    write_trajectory(record)
    return record


def main():
    cfg = fast_tier.load_env()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--container", default="opsmind-postgres")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=int(cfg.get("POSTGRES_HOST_PORT", 5433)))
    p.add_argument("--user", default=cfg.get("POSTGRES_USER"))
    p.add_argument("--password", default=cfg.get("POSTGRES_PASSWORD"))
    p.add_argument("--dbname", default=cfg.get("POSTGRES_DB"))
    p.add_argument("--window", type=int, default=30,
                   help="seconds between the two cgroup reads")
    p.add_argument("--app", default="opsmind-app",
                   help="container name of the web app that depends on the database")
    p.add_argument("--app-port", type=int, default=int(cfg.get("APP_HOST_PORT", 5050)))
    p.add_argument("--model", default=fast_tier.DEFAULT_MODEL)
    p.add_argument("--slow-model", default=slow_tier.DEFAULT_MODEL)
    p.add_argument("--live", action="store_true",
                   help="actually execute policy-approved actions (default: dry run)")
    args = p.parse_args()
    if not (args.user and args.password and args.dbname):
        sys.exit(f"missing POSTGRES_* settings: set them in {fast_tier.ENV_FILE} "
                 "or the environment")

    print(f"sampling cgroup memory/cpu for {args.window}s (trigger the fault now)...",
          file=sys.stderr)
    signal = fast_tier.collect_signal(args.container, args.host, args.port, args.user,
                                      args.password, args.dbname, args.window,
                                      app=args.app, app_port=args.app_port)
    db = dict(host=args.host, port=args.port, user=args.user, password=args.password,
              dbname=args.dbname)
    attach_running_query(signal, db)
    record = run_once(signal, args.model, args.slow_model, args.container,
                      dry_run=not args.live, db=db)
    print(f"logged run {record['run_id']} -> {TRAJECTORY_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()

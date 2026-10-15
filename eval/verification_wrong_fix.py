"""Does fault-specific verification catch a fix that did NOT fix anything?

The live eval never exercised this: every executed fix was correct, so a plain health check never
false-passed. Here we hold each real fault (the same state a wrong or no-op fix would leave behind;
no fix is applied, so no new live action is taken) and run BOTH verifiers on it:
  plain    = health_check.verify(expected_signal=None)       (container healthy + SELECT 1)
  specific = health_check.verify(expected_signal=<fault's>)  (what the supervisor really uses)
An independent fact check (eval/ground_truth.py) confirms the fault is really present first.
A false pass = verifier said verified=True while the fault was present. After cleanup the same
two verifiers run again as a CONTROL (both should pass, so "specific always fails" is ruled out).

Output: eval/results/verification_wrong_fix.jsonl (+ printed table). Never touches
trajectories/runs.jsonl or knowledge/.
Usage:  python eval/verification_wrong_fix.py   (needs Docker; injects and clears real faults)
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "eval"))

import ground_truth
import run_eval
from faults import network_partition  # noqa: F401  (load_env lives here)
from verify import health_check

OUT = ROOT / "eval" / "results" / "verification_wrong_fix.jsonl"
CONTAINER = run_eval.CONTAINER
# fault -> the expected_signal the supervisor would use for the fix that is meant to address it
SPECIFIC = {"network-partition": "network", "config-regression": "config",
            "disk-full": "disk", "dependency-down": "app"}


def both(db, app_port, signal):
    plain = health_check.verify(CONTAINER, **db, expected_signal=None, timeout=10, app_port=app_port)
    spec = health_check.verify(CONTAINER, **db, expected_signal=signal, timeout=10, app_port=app_port)
    return plain, spec


def one(fault, cfg, db, app_port):
    run_eval.restore_baseline(cfg, app_port, db)
    cmd = list(run_eval.PLAN[fault]["cmd"])
    rc, out = run_eval.sh(cmd)
    present = ground_truth.fault_present(fault, CONTAINER, db, app_port)
    row = {"fault": fault, "injector_effect": run_eval.EFFECT in out, "fault_present": present}
    if present:  # only measure on a state we independently confirmed is broken
        plain, spec = both(db, app_port, SPECIFIC[fault])
        row.update(plain_verified=plain["verified"], plain_reason=plain["reason"],
                   specific_verified=spec["verified"], specific_reason=spec["reason"],
                   plain_false_pass=bool(plain["verified"]), specific_false_pass=bool(spec["verified"]))
    run_eval.restore_baseline(cfg, app_port, db)
    time.sleep(2)
    plain_c, spec_c = both(db, app_port, SPECIFIC[fault])
    row.update(control_fault_present=ground_truth.fault_present(fault, CONTAINER, db, app_port),
               control_plain_verified=plain_c["verified"], control_specific_verified=spec_c["verified"],
               control_specific_reason=spec_c["reason"])
    return row


def main():
    cfg = network_partition.load_env()
    db = run_eval.make_db(cfg)
    app_port = int(cfg.get("APP_HOST_PORT", 5050))
    rows = []
    for fault in SPECIFIC:
        row = one(fault, cfg, db, app_port)
        row["timestamp"] = datetime.now(timezone.utc).isoformat()
        rows.append(row)
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print(f"{fault:18} present={row['fault_present']} plain_verified={row.get('plain_verified')} "
              f"specific_verified={row.get('specific_verified')} | control plain/specific="
              f"{row['control_plain_verified']}/{row['control_specific_verified']}", flush=True)
    valid = [r for r in rows if r["fault_present"]]
    print(f"\nfaults confirmed present: {len(valid)}/{len(rows)}")
    print(f"plain health check false passes:   {sum(r['plain_false_pass'] for r in valid)}/{len(valid)}")
    print(f"fault-specific check false passes: {sum(r['specific_false_pass'] for r in valid)}/{len(valid)}")


if __name__ == "__main__":
    main()

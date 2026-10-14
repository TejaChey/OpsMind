"""Evaluation runner, stage 1: the FULL OpsMind configuration on REAL injected faults.

For each (fault, trial): restore a healthy baseline, inject the real fault, collect the signal,
run the production pipeline (supervisor.run_once with knowledge ON), then score it with
eval/ground_truth.py, which is independent of the system's own verifier. Quiet controls (no
fault) measure false positives. Everything is appended to eval/results/eval_runs.jsonl and the
full signal / model outputs are kept, so baselines and ablations can be replayed offline later
(stage 2) without re-injecting. NOTHING here writes to trajectories/runs.jsonl or to knowledge/:
measuring must not change the thing being measured.

Safety: live execution only for faults whose fix is automatic AND only for action kinds the
user has approved live (APPROVED_LIVE). Anything else is forced to a dry run by a guard.

Usage:  python eval/run_eval.py [--faults f1,f2] [--trials N] [--controls N] [--pilot]
Resumable: finished (kind, fault, trial) keys in the output file are skipped.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "eval"))

import action_mapper
import fast_tier
import ground_truth
import slow_tier
import supervisor
from knowledge_experiment import GROUND_TRUTH
from tools import deploy, executor, knowledge
from verify import health_check

PY = sys.executable
OUT = ROOT / "eval" / "results" / "eval_runs.jsonl"
CONTAINER, APP = "opsmind-postgres", "opsmind-app"
APPROVED_LIVE = {"RECONNECT_NETWORK", "RESTART_DEPENDENCY", "ROLLBACK_CONFIG"}
LIVE_FAULTS = ground_truth.AUTO_FIXABLE

# mode "state": inject with --hold, then sample.  mode "event": the fault happens DURING the
# sampling window (OOM kills / CPU load are rates over a window), so start it, then sample.
PLAN = {
    "network-partition": dict(mode="state", cmd=["faults/network_partition.py", "--hold"], window=5),
    "dependency-down": dict(mode="state", cmd=["faults/dependency_down.py", "--hold"], window=5),
    "config-regression": dict(mode="state", cmd=["faults/config_regression.py", "--hold"], window=5),
    "disk-full": dict(mode="state", cmd=["faults/disk_full.py", "--hold"], window=5),
    "oom": dict(mode="event", cmd=["faults/oom.py", "--settle", "3"], window=40, pre_sleep=0),
    "cpu-spike": dict(mode="event", cmd=["faults/cpu_spike.py", "--duration", "25", "--baseline", "2"],
                      window=18, pre_sleep=3),
}
CLEAR = [["faults/network_partition.py", "--clear"], ["faults/dependency_down.py", "--clear"],
         ["faults/config_regression.py", "--clear"], ["faults/disk_full.py", "--clear"]]
EFFECT = "FAULT TOOK EFFECT"


def sh(cmd, timeout=300):
    r = subprocess.run([PY, *cmd], capture_output=True, text=True, cwd=ROOT, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def guard_executor(live_allowed):
    """Wrap executor.execute: force dry-run unless this fault allows live AND the action is one of
    the live-approved kinds. Returns a restore function."""
    orig = executor.execute

    def guarded(action, confidence, container, dry_run=True, **kw):
        really_dry = dry_run or not live_allowed or action not in APPROVED_LIVE
        return orig(action, confidence, container, dry_run=really_dry, **kw)

    executor.execute = guarded
    return lambda: setattr(executor, "execute", orig)


def make_db(cfg):
    return dict(host="localhost", port=int(cfg["POSTGRES_HOST_PORT"]), user=cfg["POSTGRES_USER"],
                password=cfg["POSTGRES_PASSWORD"], dbname=cfg["POSTGRES_DB"])


def restore_baseline(cfg, app_port, db):
    """Undo every fault and make sure the system is healthy on the baseline config."""
    for c in CLEAR:
        sh(c)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        healthy = (ground_truth.container_status(CONTAINER) == "running"
                   and ground_truth.container_status(APP) == "running"
                   and ground_truth.db_direct_ok(db))
        if healthy:
            break
        time.sleep(2)
    if deploy.read_live() != deploy.GOOD_DEFAULT:
        deploy.deploy(dict(deploy.GOOD_DEFAULT), "eval reset", app_port, kind="rollback")
    deploy.ensure_baseline(app_port)
    ok = ground_truth.app_ok(app_port) and ground_truth.db_direct_ok(db)
    if not ok:
        raise RuntimeError("could not restore a healthy baseline before the trial")


def inject_and_sample(fault, cfg, db, app_port, trial):
    """Returns (signal, injector_effect, t_fault_ready, injector_output)."""
    plan = PLAN[fault]
    cmd = list(plan["cmd"])
    if fault == "config-regression":
        cmd += ["--variant", "host" if trial % 2 else "port"]
    args = (CONTAINER, db["host"], db["port"], db["user"], db["password"], db["dbname"])
    if plan["mode"] == "state":
        t0 = time.time()
        rc, out = sh(cmd)
        t_ready = time.time()
        sig = fast_tier.collect_signal(*args, plan["window"], app=APP, app_port=app_port)
        return sig, EFFECT in out, t_ready, out
    proc = subprocess.Popen([PY, *cmd], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    t_start = time.time()
    time.sleep(plan["pre_sleep"])
    sig = fast_tier.collect_signal(*args, plan["window"], app=APP, app_port=app_port)
    out = proc.communicate(timeout=300)[0]
    return sig, EFFECT in out, t_start, out


def score(fault, record, t_ready, db, app_port, injector_effect):
    d, ex, ver = record["decision"], record.get("execution") or {}, record.get("verification")
    row = {
        "fault": fault, "injector_effect": injector_effect,
        "model_escalated": bool(record["model_raw"] and record["model_raw"]["escalate"]),
        "final_escalated": d["escalate"], "overridden": record["overridden"],
        "mapped_action": record["mapped_action"],
        "mapping_correct": record["mapped_action"] in GROUND_TRUTH[fault] if record["mapped_action"] else False,
        "exec_status": ex.get("status"), "exec_dry_run": ex.get("dry_run"),
        "verified": ver["verified"] if ver else None,
        "ttd_seconds": round(datetime.fromisoformat(record["timestamp"]).timestamp() - t_ready, 1),
    }
    if ex.get("status") == "executed":
        gt, detail = ground_truth.recovered(fault, CONTAINER, db, app_port)
        plain = health_check.verify(CONTAINER, **db, expected_signal=None, timeout=10)
        row.update(gt_recovered=gt, gt_detail=detail, plain_health_verified=plain["verified"],
                   verifier_false_pass=bool(ver and ver["verified"] and gt is False),
                   health_only_false_pass=bool(plain["verified"] and gt is False),
                   ttr_seconds=round(time.time() - t_ready, 1))
    return row


def run_trial(kind, fault, trial, cfg, db, app_port):
    live = fault in LIVE_FAULTS
    restore_baseline(cfg, app_port, db)
    restore = guard_executor(live)
    t_wall = time.time()
    try:
        if kind == "control":
            sig = fast_tier.collect_signal(CONTAINER, db["host"], db["port"], db["user"],
                                           db["password"], db["dbname"], 5, app=APP, app_port=app_port)
            t_ready, injector_effect, fault_name = t_wall, None, "none"
        else:
            sig, injector_effect, t_ready, _ = inject_and_sample(fault, cfg, db, app_port, trial)
            fault_name = fault
        supervisor.attach_running_query(sig, db)
        supervisor.attach_deploy_facts(sig)
        kctx = knowledge.context_for(sig)
        real_write = supervisor.write_trajectory
        supervisor.write_trajectory = lambda rec, path=None: real_write(rec, OUT.with_name("eval_trajectories.jsonl"))
        try:
            rec = supervisor.run_once(sig, container=CONTAINER, dry_run=not live, db=db,
                                      app_port=app_port, knowledge_context=kctx)
        finally:
            supervisor.write_trajectory = real_write
        if kind == "control":
            row = {"fault": "none", "model_escalated": bool(rec["model_raw"] and rec["model_raw"]["escalate"]),
                   "final_escalated": rec["decision"]["escalate"], "overridden": rec["overridden"],
                   "mapped_action": rec["mapped_action"]}
        else:
            row = score(fault, rec, t_ready, db, app_port, injector_effect)
        row.update(kind=kind, trial=trial, config="full", timestamp=datetime.now(timezone.utc).isoformat(),
                   knowledge_shown=bool(kctx), record=rec)
        return row
    finally:
        restore()
        restore_baseline(cfg, app_port, db)


def done_keys():
    if not OUT.exists():
        return set()
    return {(r["kind"], r["fault"], r["trial"]) for r in map(json.loads, OUT.read_text().splitlines())}


def main():
    cfg = fast_tier.load_env()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--faults", default=",".join(PLAN))
    p.add_argument("--trials", type=int, default=10)
    p.add_argument("--controls", type=int, default=10)
    p.add_argument("--pilot", action="store_true", help="1 trial per fault + 1 control")
    a = p.parse_args()
    if a.pilot:
        a.trials = a.controls = 1
    db, app_port = make_db(cfg), int(cfg.get("APP_HOST_PORT", 5050))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = done_keys()
    jobs = [("control", "none", i) for i in range(a.controls)]
    jobs += [("fault", f, i) for f in a.faults.split(",") for i in range(a.trials)]
    for kind, fault, trial in jobs:
        if (kind, fault, trial) in done:
            continue
        t0 = time.time()
        try:
            row = run_trial(kind, fault, trial, cfg, db, app_port)
        except Exception as e:   # a harness problem is recorded, never silently dropped
            row = {"kind": kind, "fault": fault, "trial": trial, "config": "full",
                   "harness_error": f"{type(e).__name__}: {e}"}
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        brief = {k: row.get(k) for k in ("injector_effect", "final_escalated", "overridden", "mapped_action",
                                          "mapping_correct", "exec_status", "verified", "gt_recovered",
                                          "harness_error")}
        print(f"[{kind} {fault} #{trial}] {round(time.time() - t0)}s {brief}", flush=True)


if __name__ == "__main__":
    main()

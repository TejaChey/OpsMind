"""Will OpsMind survive failures it was not built for? Live test on 4 NOVEL real faults
(faults/novel.py), comparing on the SAME incident:
  pipeline      the production one-shot slow tier (fixed facts, one answer)
  investigator  the agent (agents/investigator.py): same start, then it chooses read-only tools

Per trial: inject and hold the fault, check it independently, collect the production signal,
run the fast tier once (shared), then each mode -> mapper -> executor DRY RUN (nothing is ever
executed here) -> pre-registered scoring (eval/novel_ground_truth.py). For every action that
WOULD auto-run, the deterministic verifier is also run while the fault is still present: if it
says "verified", the verifier would have accepted a fix that fixed nothing (blind spot).
Also recorded: whether standard monitoring (app /health != 200) would have alerted at all.

Writes eval/results/novel_runs.jsonl (one row per trial x mode). Usage:
  python eval/novel_faults.py [--trials 3] [--faults conn-exhaust,read-only,...]
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "faults"))
sys.path.insert(0, str(ROOT / "eval"))
import action_mapper  # noqa: E402
import fast_tier  # noqa: E402
import investigator  # noqa: E402
import novel  # noqa: E402
import slow_tier  # noqa: E402
import supervisor  # noqa: E402
from novel_ground_truth import KNOWN, NOVEL, score  # noqa: E402
from tools import deploy, executor, knowledge  # noqa: E402
from verify import health_check  # noqa: E402

OUT = ROOT / "eval" / "results" / "novel_runs.jsonl"
PY = sys.executable
# Known faults are injected with their existing scripts (--hold / --clear).
KNOWN_CMDS = {
    "dependency-down": ["faults/dependency_down.py"],
    "cache-down": ["faults/dependency_down.py", "--dependency", "opsmind-redis"],
    "network-partition": ["faults/network_partition.py"],
    "config-regression": ["faults/config_regression.py"],
}


def _run(args):
    import subprocess
    p = subprocess.run([PY] + args, capture_output=True, text=True, cwd=ROOT, timeout=180)
    return (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]


class Injector:
    """Same interface for novel (faults/novel.py) and known (existing scripts) faults."""

    @staticmethod
    def inject(fault):
        return novel.inject(fault) if fault in NOVEL else _run(KNOWN_CMDS[fault] + ["--hold"])[0]

    @staticmethod
    def clear(fault):
        return novel.clear(fault) if fault in NOVEL else _run(KNOWN_CMDS[fault] + ["--clear"])[0]

    @staticmethod
    def present(fault):
        if fault in NOVEL:
            return novel.fault_present(fault)
        if fault == "dependency-down":
            return fast_tier.container_state(PG) != "running"
        if fault == "cache-down":
            return fast_tier.container_state(CACHE) != "running"
        if fault == "config-regression":
            good = deploy.last_good(deploy.read_history())
            return good is not None and deploy.read_live() != good["config"]
        if fault == "network-partition":
            import subprocess
            out = subprocess.run(["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}",
                                  PG], capture_output=True, text=True).stdout
            return "services_default" not in out
PG, APP, CACHE = "opsmind-postgres", "opsmind-app", "opsmind-redis"


def trial(fault, n, db, app_port):
    inj = Injector.inject(fault)
    time.sleep(5)
    present_before = Injector.present(fault)
    signal = fast_tier.collect_signal(PG, window=5, app=APP, app_port=app_port, cache=CACHE, **db)
    supervisor.attach_running_query(signal, db)
    supervisor.attach_deploy_facts(signal)
    kctx = knowledge.context_for(signal)
    fast = fast_tier.classify(signal)
    base = {"fault": fault, "trial": n, "at": datetime.now(timezone.utc).isoformat(),
            "injected": inj, "fault_present_before": present_before,
            "escalated": fast["decision"]["escalate"], "overridden": fast["overridden"],
            "app_health_status": signal.get("app_health_status"),
            "monitoring_would_alert": signal.get("app_health_status") != 200,
            "knowledge_shown": kctx is not None, "signal": signal}
    rows = []
    for mode in ("pipeline", "investigator"):
        row = {**base, "mode": mode}
        if not fast["decision"]["escalate"]:
            rows.append({**row, "mapped_action": None, "diagnosis_correct": False,
                         "action_correct": False, "wrong_auto_action": False})
            continue
        t = time.perf_counter()
        if mode == "pipeline":
            out = slow_tier.diagnose(fast, knowledge=kctx)
        else:
            out = investigator.investigate(
                fast, investigator.Toolbox(db, app_port, deploy.recent_deploy_facts),
                knowledge=kctx)
        row["seconds"] = round(time.perf_counter() - t, 1)
        d = out["diagnosis"]
        mapped, note = action_mapper.guard_restart_target(action_mapper.map_action(
            d["proposed_fix"]), signal)
        ex = executor.execute(mapped.value, d["confidence"], PG, True, db=db,
                              dependencies=[PG, CACHE], app_port=app_port)
        row.update({"source": out["source"], "diagnosis": d, "mapped_action": mapped.value,
                    "guard_note": note, "exec_status": ex["status"], "exec_reason": ex["reason"],
                    "trace": out.get("trace"), "forced_conclusion": out.get("forced_conclusion"),
                    "challenged": any(a.get("challenged") for a in out.get("attempts", []))})
        row.update(score(fault, d["root_cause"], mapped.value, ex["status"]))
        if ex["status"] == "would_execute":
            v = health_check.verify(PG, **db, timeout=20,
                                    expected_signal=supervisor.EXPECTED_SIGNAL.get(mapped.value),
                                    app_port=app_port)
            row["verifier_passes_with_fault_present"] = v["verified"]
            row["verifier_reason"] = v["reason"]
        rows.append(row)
    present_after = Injector.present(fault)
    for r in rows:
        r["fault_present_after"] = present_after
        r["fault_kind"] = "novel" if fault in NOVEL else "known"
    print(Injector.clear(fault), flush=True)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--faults", default=",".join(NOVEL),
                   help="any of: " + ", ".join(list(NOVEL) + list(KNOWN)))
    p.add_argument("--out", default=str(OUT), help="results file (appended)")
    a = p.parse_args()
    out_path = Path(a.out)
    e = fast_tier.load_env()
    db = dict(host="localhost", port=int(e["POSTGRES_HOST_PORT"]), user=e["POSTGRES_USER"],
              password=e["POSTGRES_PASSWORD"], dbname=e["POSTGRES_DB"])
    app_port = int(e.get("APP_HOST_PORT", 5050))
    deploy.ensure_baseline(app_port)
    faults = a.faults.split(",")
    for n in range(1, a.trials + 1):          # round-robin so time drift hits every fault
        for fault in faults:
            try:
                rows = trial(fault, n, db, app_port)
            except Exception as ex:           # record and clean up; never leave a fault behind
                rows = [{"fault": fault, "trial": n, "harness_error": f"{type(ex).__name__}: {ex}"}]
                print(Injector.clear(fault), flush=True)
            for r in rows:
                with out_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(r, default=str) + "\n")
                if "harness_error" in r:
                    print(f"[{fault} #{n}] HARNESS ERROR {r['harness_error']}", flush=True)
                else:
                    print(f"[{fault} #{n}] {r['mode']:12} esc={r['escalated']} "
                          f"diag={r['diagnosis_correct']} action={r['mapped_action']} "
                          f"ok={r['action_correct']} wrong_auto={r['wrong_auto_action']} "
                          f"verifier_false_pass={r.get('verifier_passes_with_fault_present')} "
                          f"| {(r.get('diagnosis') or {}).get('root_cause', '')[:90]}", flush=True)


if __name__ == "__main__":
    main()

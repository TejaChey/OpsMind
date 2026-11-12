"""v1 pipeline vs v2 multi-agent graph, on the same real incidents.

DRY (default): per trial, inject and hold a fault, collect ONE signal, then run
  pipeline  the production v1 path (fast tier + floor, one-shot slow tier, mapper, policy)
  graph     the v2 LangGraph multi-agent graph (opsgraph), dry run, given the same signal
and score both in code with the pre-registered rules in eval/novel_ground_truth.py.

LIVE (--live): the graph alone, really executing what its gates allow (governor off: trials
repeat on purpose), then recovery is judged independently of the verifier: the fault's own
presence check (eval/novel_faults.Injector.present) must say gone AND the app must answer 200
with both dependencies OK.

Rows go to eval/results/graph_eval_{dry,live}.jsonl. Summary: python eval/graph_eval.py --summary
"""
import argparse
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "agents"), str(ROOT / "eval"), str(ROOT / "faults")]
import action_mapper  # noqa: E402
import fast_tier  # noqa: E402
import slow_tier  # noqa: E402
import supervisor  # noqa: E402
from novel_faults import CACHE, PG, APP, Injector  # noqa: E402
from novel_ground_truth import NOVEL, NOVEL_AND_KNOWN, KNOWN  # noqa: E402
from opsgraph.graph import build, default_cfg, new_run_id  # noqa: E402
from summarize_novel import STRICT  # noqa: E402
from tools import deploy, executor, knowledge  # noqa: E402

RES = ROOT / "eval" / "results"
FAULTS = list(KNOWN) + list(NOVEL)
LIVE_FAULTS = ["cache-down", "dependency-down", "network-partition", "config-regression", "compound"]


def diag_ok(fault, text):
    return all(re.search(p, text or "", re.I) for p in STRICT[fault])


def score_steps(fault, steps):
    """steps: [(action, would_auto_run)]. Correct = every proposed action is acceptable for the
    fault; wrong_auto = some unacceptable action would run on its own."""
    ok_set = NOVEL_AND_KNOWN[fault]["correct_actions"]
    actions = [a for a, _ in steps]
    auto = [a for a, run in steps if run]
    return {"action_correct": bool(actions) and all(a in ok_set for a in actions),
            # defined before the clean run: what would run by itself is all correct (an extra
            # "flag for a person" step alongside a correct automatic fix is not penalised)
            "auto_steps_correct": bool(auto) and all(a in ok_set for a in auto),
            "wrong_auto_action": any(auto and a not in ok_set for a, auto in steps),
            "full_fix_planned": fault != "compound" or
            {"RESTART_DEPENDENCY", "ROLLBACK_CONFIG"} <= {a for a, auto in steps if auto}}


def pipeline(signal, db, app_port):
    t = time.perf_counter()
    fast = fast_tier.classify(signal)
    if not fast["decision"]["escalate"]:
        return {"escalated": False, "seconds": round(time.perf_counter() - t, 1)}
    kctx = knowledge.context_for(signal)
    d = slow_tier.diagnose(fast, knowledge=kctx)["diagnosis"]
    mapped, _ = action_mapper.guard_restart_target(action_mapper.map_action(d["proposed_fix"]), signal)
    ex = executor.execute(mapped.value, d["confidence"], PG, True, db=db,
                          dependencies=[PG, CACHE], app_port=app_port)
    return {"escalated": True, "root_cause": d["root_cause"], "steps": [(mapped.value, ex["status"] == "would_execute")],
            "seconds": round(time.perf_counter() - t, 1)}


def graph_run(graph, signal, cfg):
    run_id = new_run_id()
    t = time.perf_counter()
    out = graph.invoke({"run_id": run_id, "cfg": cfg, "revisions": 0, "signal": signal},
                       {"configurable": {"thread_id": run_id}})
    res = {"run_id": run_id, "seconds": round(time.perf_counter() - t, 1),
           "escalated": out["triage"]["decision"]["escalate"], "outcome": out.get("outcome")}
    if res["escalated"]:
        res.update({"root_cause": out["diagnosis"]["root_cause"],
                    "steps": [(s["action"], s["decision"] == "auto") for s in out["plan"]],
                    "decisions": [s["decision"] for s in out["plan"]],
                    "findings": [{k: f[k] for k in ("component", "status", "grounded")} for f in out["findings"]],
                    "revisions": len(out.get("critiques", [])) - 1,
                    "code_objections": sum(bool(c.get("code_objections")) for c in out.get("critiques", [])),
                    "executions": [{k: e.get(k) for k in ("action", "status", "reason")} for e in out.get("executions") or []],
                    "verification": out.get("verification")})
    return res


def app_ok(port):
    status, _, body = fast_tier.probe_app(port, timeout=5)
    return status == 200 and body.get("db_ok") is True and body.get("redis_ok") is True


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--live", action="store_true")
    p.add_argument("--faults", default=None)
    p.add_argument("--summary", action="store_true")
    a = p.parse_args()
    if a.summary:
        return summary()
    cfg0 = default_cfg()
    db, port = cfg0["db"], cfg0["app_port"]
    deploy.ensure_baseline(port)
    graph = build()
    faults = a.faults.split(",") if a.faults else (LIVE_FAULTS if a.live else FAULTS)
    out = RES / f"graph_eval_{'live' if a.live else 'dry'}.jsonl"
    for n in range(1, a.trials + 1):
        for fault in faults:
            row = {"fault": fault, "trial": n, "kind": "novel" if fault in NOVEL else "known",
                   "at": datetime.now(timezone.utc).isoformat()}
            try:
                row["injected"] = str(Injector.inject(fault))[-200:]
                time.sleep(5)
                row["fault_present_before"] = Injector.present(fault)
                signal = fast_tier.collect_signal(PG, window=5, app=APP, app_port=port, cache=CACHE, **db)
                supervisor.attach_running_query(signal, db)
                supervisor.attach_deploy_facts(signal)
                if a.live:
                    cfg = default_cfg(dry_run=False, governor=False, record=False)
                    row["graph"] = graph_run(graph, signal, cfg)
                    time.sleep(3)
                    row["gt_fault_gone"] = Injector.present(fault) is False
                    row["gt_app_ok"] = app_ok(port)
                    row["gt_recovered"] = row["gt_fault_gone"] and row["gt_app_ok"]
                    v = (row["graph"].get("verification") or {}).get("verified")
                    row["verifier_false_pass"] = v is True and not row["gt_recovered"]
                else:
                    row["pipeline"] = pipeline(signal, db, port)
                    row["graph"] = graph_run(graph, signal, default_cfg(dry_run=True, record=False))
                for m in ("pipeline", "graph"):
                    r = row.get(m)
                    if r and r.get("escalated"):
                        r.update(score_steps(fault, r["steps"]))
                        r["diagnosis_correct"] = diag_ok(fault, r["root_cause"])
            except Exception as e:
                row["harness_error"] = f"{type(e).__name__}: {e}"
            finally:
                try:
                    row["cleared"] = str(Injector.clear(fault))[-120:]
                except Exception as e:
                    row["clear_error"] = str(e)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
            g = row.get("graph") or {}
            print(f"[{fault} #{n}] graph: steps={g.get('steps')} decisions={g.get('decisions')} "
                  f"diag={g.get('diagnosis_correct')} ok={g.get('action_correct')} "
                  f"wrong_auto={g.get('wrong_auto_action')} {g.get('seconds')}s"
                  + (f" | recovered={row.get('gt_recovered')} verified={(g.get('verification') or {}).get('verified')}"
                     if a.live else
                     f" | pipeline: {(row.get('pipeline') or {}).get('steps')} ok={(row.get('pipeline') or {}).get('action_correct')}")
                  + (f" ERROR {row['harness_error']}" if "harness_error" in row else ""), flush=True)


def summary():
    for kind in ("dry", "live"):
        path = RES / f"graph_eval_{kind}.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        ok = [r for r in rows if "harness_error" not in r and r.get("fault_present_before") is not False]
        print(f"\n== {kind}: {len(rows)} trials, {len(rows) - len(ok)} excluded")
        for m in (("pipeline", "graph") if kind == "dry" else ("graph",)):
            for group in ("known", "novel"):
                rs = [r[m] for r in ok if r["kind"] == group and r.get(m)]
                if not rs:
                    continue
                k = lambda f: sum(1 for r in rs if f(r))
                print(f"  {m:<9} {group:<6} n={len(rs):<3} detected {k(lambda r: r.get('escalated'))}"
                      f"  diagnosis {k(lambda r: r.get('diagnosis_correct'))}"
                      f"  correct action {k(lambda r: r.get('action_correct'))}"
                      f"  auto steps correct {k(lambda r: r.get('auto_steps_correct'))}"
                      f"  wrong auto {k(lambda r: r.get('wrong_auto_action'))}"
                      f"  median {statistics.median([r['seconds'] for r in rs])}s")
        per = defaultdict(list)
        for r in ok:
            per[r["fault"]].append(r)
        for fault, rs in per.items():
            line = f"    {fault:<18}"
            for m in (("pipeline", "graph") if kind == "dry" else ("graph",)):
                c = sum(1 for r in rs if (r.get(m) or {}).get("action_correct"))
                line += f" {m} {c}/{len(rs)}"
            if kind == "live":
                line += (f" recovered {sum(1 for r in rs if r.get('gt_recovered'))}/{len(rs)}"
                         f" verifier_false_pass {sum(1 for r in rs if r.get('verifier_false_pass'))}")
            if fault == "compound":
                line += f" full-fix planned {sum(1 for r in rs if (r.get('graph') or {}).get('full_fix_planned'))}/{len(rs)}"
            print(line)


if __name__ == "__main__":
    main()

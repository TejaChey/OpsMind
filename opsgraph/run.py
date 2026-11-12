"""Run the multi-agent incident graph once from the command line.

  python -m opsgraph.run                 # dry run: agents investigate and plan, nothing changes
  python -m opsgraph.run --live          # policy-approved steps really execute (governor on)
  python -m opsgraph.run --live --hitl   # steps that need a person pause here for y/n

Every event is written to trajectories/graph/<run_id>.jsonl (the console replays these).
"""
import argparse
import json
import sys

from langgraph.types import Command

from .graph import build, default_cfg, new_run_id


def run(cfg, signal=None, run_id=None, on_interrupt=None, graph=None):
    """Run to completion. on_interrupt(payload) -> resume value (e.g. {"approve": [0]})."""
    run_id = run_id or new_run_id()
    graph = graph or build()
    thread = {"configurable": {"thread_id": run_id}}
    state = {"run_id": run_id, "cfg": cfg, "revisions": 0}
    if signal is not None:
        state["signal"] = signal
    result = graph.invoke(state, thread)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        answer = on_interrupt(payload) if on_interrupt else {"approve": [], "by": "auto-reject"}
        result = graph.invoke(Command(resume=answer), thread)
    return run_id, result


def _ask(payload):
    approve = []
    for s in payload["steps"]:
        failed = [c for c in s["checks"] if not c["ok"]]
        print(f"\nstep {s['step']}: {s['fix']} -> {s['action']}")
        for c in failed:
            print(f"   blocked by {c['check']}: {c['detail']}")
        if input("   approve this step? [y/N] ").strip().lower() == "y":
            approve.append(s["step"])
    return {"approve": approve, "by": "cli"}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--live", action="store_true")
    p.add_argument("--hitl", action="store_true", help="ask a person about blocked steps")
    p.add_argument("--no-governor", action="store_true")
    p.add_argument("--no-knowledge", action="store_true")
    p.add_argument("--window", type=int, default=5)
    a = p.parse_args()
    cfg = default_cfg(dry_run=not a.live, hitl=a.hitl, governor=not a.no_governor,
                      knowledge=not a.no_knowledge, window=a.window)
    run_id, result = run(cfg, on_interrupt=_ask if a.hitl else None)
    d = result.get("diagnosis") or {}
    print(f"\nrun {run_id}")
    for f in result.get("findings") or []:
        print(f"  [{f['component']:<11}] {f['status']:<8} grounded={f['grounded']} "
              f"{f['summary'][:90]}")
    if d:
        print(f"  root cause: {d['root_cause']}")
    for s in result.get("plan") or []:
        print(f"  step {s['step']}: {s['action']:<20} -> {s['decision']}")
    for e in result.get("executions") or []:
        print(f"  executed: {e['action']} {e['status']}")
    v = result.get("verification")
    if v:
        print(f"  verified: {v['verified']} ({v['reason']})")
    print(f"  outcome: {json.dumps(result.get('outcome'))}")
    print(f"  events: trajectories/graph/{run_id}.jsonl", file=sys.stderr)


if __name__ == "__main__":
    main()

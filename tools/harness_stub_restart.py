"""TEST HARNESS ONLY. NOT part of the real agent pipeline.

Forces the slow tier to say "Restart the service" (confidence 0.9) so the executor path for
RESTART_CONTAINER can be exercised. The real OOM case maps to INCREASE_MEMORY_LIMIT (blocked
by policy.yaml), so it never reaches a restart on its own.

What is real: the signal (copied from the latest run in trajectories/runs.jsonl), the fast
tier, the mapper, the policy check, and the executor. What is fake: the slow tier's answer.

Output goes ONLY to trajectories/harness_runs.jsonl, with "HARNESS_TEST": true and a note, so
a forced test is never mistaken for genuine model behavior. runs.jsonl is only read.

Usage:  python tools/harness_stub_restart.py          # dry run (default): nothing executes
        python tools/harness_stub_restart.py --live   # really restarts the container
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT))

import fast_tier  # noqa: E402
import slow_tier  # noqa: E402
import supervisor  # noqa: E402

RUNS_FILE = ROOT / "trajectories" / "runs.jsonl"
HARNESS_FILE = ROOT / "trajectories" / "harness_runs.jsonl"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--container", default="opsmind-postgres")
    p.add_argument("--fix", default="Restart the service",
                   help="the text the stubbed slow tier 'proposes' (e.g. 'Terminate the "
                        "longest-running query' to exercise TERMINATE_QUERY)")
    p.add_argument("--live", action="store_true", help="actually execute (default: dry run)")
    args = p.parse_args()

    lines = RUNS_FILE.read_text(encoding="utf-8").splitlines()
    if not lines:
        sys.exit(f"no runs in {RUNS_FILE} to copy a signal from")
    src = json.loads(lines[-1])

    # The stub: replaces the slow tier's model call with a fixed answer.
    slow_tier.diagnose = lambda fast_result, model: {
        "diagnosis": {"root_cause": "STUB (forced by harness)",
                      "proposed_fix": args.fix, "confidence": 0.9},
        "source": "stub", "model": "stub", "attempts": []}

    # Redirect logging. Pass the path explicitly: write_trajectory's default argument was
    # bound at import time, so changing supervisor.TRAJECTORY_FILE would not redirect it.
    real_write = supervisor.write_trajectory
    note = (f"FORCED TEST, NOT A REAL INCIDENT: slow tier stubbed to say '{args.fix}' "
            f"(confidence 0.9); signal copied from real run {src['run_id']}. "
            f"{'LIVE' if args.live else 'DRY RUN'}. Not genuine model behavior.")
    supervisor.write_trajectory = lambda rec, path=None: real_write(
        {"HARNESS_TEST": True, "note": note, **rec}, HARNESS_FILE)

    cfg = fast_tier.load_env()
    db = dict(host="localhost", port=int(cfg.get("POSTGRES_HOST_PORT", 5433)),
              user=cfg.get("POSTGRES_USER"), password=cfg.get("POSTGRES_PASSWORD"),
              dbname=cfg.get("POSTGRES_DB"))
    out = supervisor.run_once(src["signal"], container=args.container,
                              dry_run=not args.live, db=db)
    print(json.dumps(out["execution"], indent=1))
    print(f"logged harness run {out['run_id']} -> {HARNESS_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()

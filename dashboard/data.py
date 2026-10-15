"""Read-only data helpers for the dashboard. No Streamlit imports, so tests can run without it.

Every number shown on the dashboard is computed here in code from the trajectory file;
nothing is estimated or summarised by a model.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "trajectories" / "runs.jsonl"
HARNESS = ROOT / "trajectories" / "harness_runs.jsonl"


def load_jsonl(path):
    """Return a list of dicts. Skips blank lines; a corrupt line is kept as an error row, not hidden."""
    rows = []
    path = Path(path)
    if not path.exists():
        return rows
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            rows.append({"run_id": f"corrupt-line-{n}", "action": "CORRUPT", "error": str(e)})
    return rows


def run_summary(run):
    """One flat row per run, for the table."""
    ex = run.get("execution") or {}
    ver = run.get("verification") or {}
    dec = run.get("decision") or {}
    return {
        "run_id": run.get("run_id", "")[:8],
        "time": (run.get("timestamp") or "")[:19].replace("T", " "),
        "action": run.get("action"),
        "severity": dec.get("severity"),
        "overridden": bool(run.get("overridden")),
        "mapped_action": run.get("mapped_action"),
        "exec_status": ex.get("status"),
        "dry_run": ex.get("dry_run"),
        "verified": ver.get("verified") if ver else None,
    }


def stats(runs):
    """Headline numbers. Counts only; the denominators are always shown next to them."""
    escalated = [r for r in runs if r.get("action") == "escalate_slow_tier"]
    overridden = [r for r in runs if r.get("overridden")]
    live = [r for r in runs if (r.get("execution") or {}).get("status") == "executed"
            and not (r.get("execution") or {}).get("dry_run")]
    verified = [r for r in live if (r.get("verification") or {}).get("verified") is True]
    return {
        "total_runs": len(runs),
        "escalated": len(escalated),
        "overridden": len(overridden),
        "live_executed": len(live),
        "live_verified": len(verified),
        "with_knowledge": sum(1 for r in runs if r.get("knowledge_context")),
    }


def mapped_action_counts(runs):
    counts = {}
    for r in runs:
        a = r.get("mapped_action")
        if a:
            counts[a] = counts.get(a, 0) + 1
    return counts

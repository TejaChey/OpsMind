"""Human hand-off: decide in code whether an incident run needs a person, and queue it.

Before this, a run that ended in "blocked pending human approval", a failed fix or a refused
repeat was only a line in runs.jsonl that nobody was told about. Now each such run adds one
entry to trajectories/escalations.jsonl (the queue an on-call person, or a pager/chat adapter,
reads): why a human is needed, what the system saw and proposed, and what it already did, so
the person does not start from zero. Writing a queue file instead of calling a paging service
keeps this fully local; a real deployment would forward each new line to its pager.
"""
from datetime import datetime, timezone
from pathlib import Path

from tools.jsonl_io import append_jsonl

QUEUE_FILE = Path(__file__).resolve().parent.parent / "trajectories" / "escalations.jsonl"

# execution status -> why a person is needed
REASONS = {
    "blocked_by_governor": "automation limits refused the action (paused, or a repeat; see detail)",
    "blocked_pending_human_approval": "the proposed action needs human approval",
    "failed": "the action was attempted and failed",
    "not_implemented": "the proposed action has no executor",
    "no_target": "the action found nothing to act on, but the incident was escalated",
}


def needs_human(record):
    """Returns the reason a person must look at this run, or None. Pure function of the record."""
    ex = record.get("execution")
    if not ex:   # fast tier did not escalate: nothing is wrong
        return None
    if ex["status"] in REASONS:
        return REASONS[ex["status"]]
    if ex["status"] == "executed" and (record.get("verification") or {}).get("verified") is not True:
        return "the fix ran but deterministic verification did not pass"
    return None   # verified fix, or a dry run the operator is watching


def handoff(record, reason):
    d = (record.get("slow_tier") or {}).get("diagnosis") or {}
    ex = record.get("execution") or {}
    return {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "run_id": record["run_id"],
        "reason": reason,
        "detail": ex.get("reason"),
        "severity": (record.get("decision") or {}).get("severity"),
        "root_cause": d.get("root_cause"),
        "proposed_fix": d.get("proposed_fix"),
        "mapped_action": record.get("mapped_action"),
        "execution_status": ex.get("status"),
        "verified": (record.get("verification") or {}).get("verified"),
        "governor": record.get("governor"),
        "trajectory": f"trajectories/runs.jsonl#{record['run_id']}",
    }


def escalate(record, path=QUEUE_FILE):
    """Queue the run if it needs a person. Returns the queued entry or None."""
    reason = needs_human(record)
    if reason is None:
        return None
    entry = handoff(record, reason)
    append_jsonl(path, entry)
    return entry

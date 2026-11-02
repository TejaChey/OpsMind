"""CI domain: free-text fix -> one safe action (mapper), policy-gated execution with targets
chosen in CODE, and deterministic verification. Same rules as the Docker domain: the model
never picks what gets touched, and whether a fix worked is decided by a fresh pipeline run.
"""
import enum
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

from action_mapper import NEGATION
from ci import ci_signal, pipeline
from tools import executor


class CiAction(enum.Enum):
    RERUN_PIPELINE = "RERUN_PIPELINE"
    REVERT_COMMIT = "REVERT_COMMIT"
    FIX_CODE = "FIX_CODE"
    FIX_DEPENDENCY = "FIX_DEPENDENCY"
    UNKNOWN_NEEDS_HUMAN = "UNKNOWN_NEEDS_HUMAN"


RULES = {
    CiAction.RERUN_PIPELINE: [
        r"\b(re-?run|retry|re-?trigger|re-?start|re-?execute)\b.{0,30}"
        r"\b(pipeline|job|build|workflow|step|ci|failed)\b"],
    CiAction.REVERT_COMMIT: [
        r"\b(revert|roll ?back|undo|back out)\b.{0,40}\b(commit|change|merge|patch|push)\b"],
    CiAction.FIX_DEPENDENCY: [
        r"\b(install|add|restore|pin|reinstall|provide)\b.{0,40}"
        r"\b(dependenc\w*|package|module|librar\w*|requirement\w*)\b"],
    CiAction.FIX_CODE: [
        r"\b(fix|correct|repair|patch|update|rewrite)\b.{0,40}"
        r"\b(code|tests?|function|bug|syntax|implementation|calculation)\b"],
}


def map_action(proposed_fix):
    """Exactly one CiAction for the free text; conflicts, negations and no match -> a human."""
    if not isinstance(proposed_fix, str) or not proposed_fix.strip():
        return CiAction.UNKNOWN_NEEDS_HUMAN
    text = proposed_fix.lower()
    if NEGATION.search(text):
        return CiAction.UNKNOWN_NEEDS_HUMAN
    matched = [a for a, pats in RULES.items() if any(re.search(p, text) for p in pats)]
    return matched[0] if len(matched) == 1 else CiAction.UNKNOWN_NEEDS_HUMAN


# ---------- executor ----------

def _rerun_target(signal):
    """(ok, why). An unchanged re-run only helps a failure that can be transient. Re-running a
    deterministic failure (missing module, failing test) wastes a cycle and hides the problem."""
    if signal.get("error_class") in ci_signal.TRANSIENT:
        return True, f"{signal['error_class']} can be transient"
    return False, f"error_class={signal.get('error_class')!r} is deterministic: a re-run cannot help"


def _revert_target(signal):
    """(sha or None, why). Revert HEAD only if it is the single commit after the last green one
    and the failure is not an outage that no revert could cause."""
    if signal.get("error_class") in ci_signal.TRANSIENT:
        return None, "failure looks transient; reverting a commit would not address it"
    if signal.get("commits_since_green") != 1:
        return None, f"commits_since_green={signal.get('commits_since_green')}, need exactly 1"
    try:
        return pipeline.head_sha(), "HEAD is the only commit after the last green one"
    except RuntimeError as e:
        return None, str(e)


def execute(action, confidence, signal, dry_run=True, policy=None):
    """Returns {action, status, reason, dry_run, ...}. Statuses: blocked_pending_human_approval,
    would_execute, no_target, executed, failed, not_implemented. Never raises."""
    out = {"action": action, "dry_run": dry_run}
    try:
        policy = policy if policy is not None else executor.load_policy()
    except Exception as e:
        return {**out, "status": "blocked_pending_human_approval",
                "reason": f"could not load policy.yaml: {e}"}
    allowed, reason = executor.check_policy(action, confidence, policy)
    if not allowed:
        return {**out, "status": "blocked_pending_human_approval", "reason": reason}
    try:
        if action == "RERUN_PIPELINE":
            ok, why = _rerun_target(signal)
            if not ok:
                return {**out, "status": "no_target", "reason": why}
            if dry_run:
                return {**out, "status": "would_execute", "reason": f"would re-run: {why}"}
            res = pipeline.run_pipeline()
            return {**out, "status": "executed", "reason": f"re-ran the pipeline: {why}",
                    "rerun_run_id": res["run_id"], "rerun_conclusion": res["conclusion"]}
        if action == "REVERT_COMMIT":
            target, why = _revert_target(signal)
            if target is None:
                return {**out, "status": "no_target", "reason": why}
            if dry_run:
                return {**out, "status": "would_execute", "reason": f"would revert {target[:10]}",
                        "target_sha": target}
            pipeline.git("revert", "--no-edit", target)
            return {**out, "status": "executed", "reason": f"reverted {target[:10]}",
                    "target_sha": target, "new_head": pipeline.head_sha()}
    except Exception as e:   # git / subprocess problems
        return {**out, "status": "failed", "reason": f"{type(e).__name__}: {e}"}
    return {**out, "status": "not_implemented", "reason": f"{action} has no executor (human-only)"}


# ---------- verification (deterministic, never the model) ----------

def verify(action, last_green_sha=None):
    """Run the whole pipeline fresh. Verified only if it is green; after a revert the tree must
    also equal the last green commit's tree (fail closed if that cannot be checked)."""
    res = pipeline.run_pipeline()
    out = {"verified": res["conclusion"] == "success", "pipeline_conclusion": res["conclusion"],
           "verify_run_id": res["run_id"], "failed_step_after": res["failed_step"]}
    if action == "REVERT_COMMIT":
        r = subprocess.run(["git", "diff", "--quiet", last_green_sha or "", "HEAD"],
                           cwd=pipeline.WORKSPACE, capture_output=True)
        out["tree_equals_last_green"] = (r.returncode == 0) if last_green_sha else False
        out["verified"] = out["verified"] and out["tree_equals_last_green"]
    out["reason"] = ("pipeline green" if out["verified"] else
                     f"pipeline {res['conclusion']}" + (f" at {res['failed_step']}" if res["failed_step"] else "")
                     + ("; tree check failed" if action == "REVERT_COMMIT" and not out.get("tree_equals_last_green") else ""))
    return out

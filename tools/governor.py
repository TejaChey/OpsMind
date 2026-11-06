"""Governor: limits on automation that hold ACROSS incidents (policy.yaml judges one incident).

Why this exists: in published outage write-ups the automation often made things worse by
repeating itself, e.g. restarting the same thing in a loop or re-applying a fix that did not work.
policy.yaml only asks whether one action is allowed for one incident. The governor also looks at
what OpsMind already did recently (the real, live executions in trajectories/runs.jsonl) and
refuses to act again when:

  1. automation is paused: a file named OPSMIND_PAUSED exists in the repo root (the fastest
     kill switch for an on-call person: `touch OPSMIND_PAUSED`), or policy.yaml governor
     `automation_enabled: false`
  2. the same action on the same target FAILED verification within the window (the fix did not
     work last time, so doing it again is guessing)
  2b. a deploy is in progress (deploys/IN_PROGRESS exists: its health is not decided yet), so
     acting now would race the deploy (change freeze)
  3. the same action on the same target already ran `max_executions_per_target` times within the
     window, even if each one verified (the fix is not lasting: it treats a symptom, which a
     health check a few seconds later cannot see)

Everything is decided in code from the logged history; a refusal escalates to a human. History
that cannot be read counts as a refusal (fail closed). Dry runs and harness tests are not
counted (they changed nothing).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PAUSE_FILE = ROOT / "OPSMIND_PAUSED"
DEPLOY_LOCK = ROOT / "deploys" / "IN_PROGRESS"
HISTORY_FILE = ROOT / "trajectories" / "runs.jsonl"
POLICY_FILE = ROOT / "policy.yaml"
DEFAULTS = {"automation_enabled": True, "window_minutes": 30, "max_executions_per_target": 2}


def load_config(path=POLICY_FILE):
    with Path(path).open(encoding="utf-8") as f:
        return {**DEFAULTS, **(yaml.safe_load(f).get("governor") or {})}


def load_history(path=HISTORY_FILE):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def targets_of(execution):
    """The container(s) an execution acted on. RESTART_DEPENDENCY records the dependencies it
    really started (chosen in code); every other action acts on the monitored container."""
    if execution.get("action") == "RESTART_DEPENDENCY":
        return set(execution.get("started") or execution.get("targets") or [])
    return {execution.get("container")}


def recent_live_executions(history, action, now, window_minutes):
    """Real executions of `action` within the window, newest first."""
    since = now - timedelta(minutes=window_minutes)
    out = []
    for r in history:
        ex = r.get("execution") or {}
        if ex.get("status") != "executed" or ex.get("dry_run") is not False:
            continue
        if ex.get("action") != action or r.get("HARNESS_TEST"):
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts >= since:
            out.append((ts, r))
    return [r for _, r in sorted(out, key=lambda x: x[0], reverse=True)]


def check(action, targets, history=None, now=None, config=None, pause_file=PAUSE_FILE,
          deploy_lock=DEPLOY_LOCK):
    """Returns (allowed, reason, evidence). Call it only for an action that would otherwise
    really execute. `targets` = the container name(s) the action would act on now (for
    RESTART_DEPENDENCY: the dependencies that are not running); a past execution counts if it
    acted on any of them."""
    targets = {targets} if isinstance(targets, str) else set(targets)
    try:
        config = config if config is not None else load_config()
        history = history if history is not None else load_history()
    except (OSError, ValueError, yaml.YAMLError) as e:
        return False, f"governor could not read its config or history ({e}); failing closed", {}
    now = now or datetime.now(timezone.utc)
    if Path(pause_file).exists():
        return False, f"automation paused ({Path(pause_file).name} exists)", {"paused": True}
    if config.get("automation_enabled") is not True:
        return False, "automation paused (policy.yaml governor automation_enabled is not true)", \
            {"paused": True}
    if Path(deploy_lock).exists():
        try:
            info = Path(deploy_lock).read_text(encoding="utf-8").strip()
        except OSError:
            info = "unreadable"
        return False, (f"change freeze: a deploy is in progress ({info}); if no deploy is "
                       f"running, a person must remove {Path(deploy_lock).name}"), {"frozen": True}
    window = config["window_minutes"]
    recent = recent_live_executions(history, action, now, window)
    recent = [r for r in recent if targets_of(r["execution"]) & targets]
    evidence = {"window_minutes": window, "recent_executions": len(recent),
                "recent_run_ids": [r["run_id"] for r in recent]}
    failed = [r for r in recent if (r.get("verification") or {}).get("verified") is not True]
    if failed:
        return False, (f"{action} on {sorted(targets_of(failed[0]['execution']))} did not verify in run "
                       f"{failed[0]['run_id'][:8]} within the last {window} min; not repeating "
                       "a fix that did not work"), evidence
    limit = config["max_executions_per_target"]
    if len(recent) >= limit:
        return False, (f"{action} already ran {len(recent)} times within {window} min (limit "
                       f"{limit}); the fault keeps coming back, so the fix is not lasting"), evidence
    return True, "within governor limits", evidence

"""Executor: apply a mapped action, but only if policy.yaml allows it.

Order of checks (all in code, fail closed):
  1. action missing from policy / auto_approve false / confidence below threshold
       -> status "blocked_pending_human_approval", nothing happens
  2. dry_run (the default) -> status "would_execute", nothing happens
  3. otherwise run the action. RESTART_CONTAINER, TERMINATE_QUERY and RECONNECT_NETWORK are
     implemented; any other approved action is reported as "not_implemented" and does nothing.

TERMINATE_QUERY needs `db` (host/port/user/password/dbname) to reach Postgres. In dry-run it
still looks up (read-only) which query it WOULD end, so the log shows the real target.
"""
import time
from pathlib import Path

import psycopg2
import yaml

POLICY_FILE = Path(__file__).resolve().parent.parent / "policy.yaml"


def load_policy(path=POLICY_FILE):
    with Path(path).open(encoding="utf-8") as f:
        return yaml.safe_load(f)["actions"]


def check_policy(action, confidence, policy):
    """Returns (allowed, reason)."""
    rule = policy.get(action)
    if rule is None:
        return False, f"{action} is not listed in policy.yaml"
    if rule.get("auto_approve") is not True:
        return False, f"{action} has auto_approve: false"
    if confidence < rule["min_confidence"]:
        return False, f"confidence {confidence} < min_confidence {rule['min_confidence']}"
    return True, "policy allows auto-execution"


def restart_container(name):
    """Restart via the Docker SDK. Returns before/after StartedAt as mechanical evidence."""
    import docker
    c = docker.from_env().containers.get(name)
    before = c.attrs["State"]["StartedAt"]
    c.restart(timeout=30)
    c.reload()
    return {"started_at_before": before, "started_at_after": c.attrs["State"]["StartedAt"]}


# Longest-running active client query on this database, excluding our own connection.
# Parallel workers are skipped on purpose: ending their leader backend ends them too.
TARGET_SQL = """
SELECT pid, usename, state,
       EXTRACT(EPOCH FROM clock_timestamp() - query_start)::float AS running_seconds,
       left(query, 200) AS query
FROM pg_stat_activity
WHERE state = 'active' AND backend_type = 'client backend'
  AND datname = current_database() AND pid <> pg_backend_pid()
ORDER BY query_start ASC
LIMIT 1
"""


def _connect(db):
    conn = psycopg2.connect(connect_timeout=5, **db)
    conn.autocommit = True
    return conn


def find_longest_query(cur):
    """The longest-running active query as a dict, or None."""
    cur.execute(TARGET_SQL)
    row = cur.fetchone()
    if row is None:
        return None
    pid, user, state, secs, query = row
    return {"pid": pid, "usename": user, "state": state,
            "running_seconds": round(secs, 2), "query": query}


def terminate_query(db, dry_run, settle=5):
    """Returns (status, reason, evidence). Live: pg_terminate_backend, then confirm the pid is
    really gone from pg_stat_activity (not just that the call returned true)."""
    if not db:
        return "failed", "no database connection settings given", {}
    try:
        conn = _connect(db)
    except psycopg2.Error as e:
        return "failed", f"cannot connect to Postgres: {str(e).strip()}", {}
    try:
        with conn.cursor() as cur:
            target = find_longest_query(cur)
            if target is None:
                return "no_target", "no active query found to terminate", {}
            evidence = {"target_before": target}
            if dry_run:
                return "would_execute", (f"would terminate pid {target['pid']} (running "
                                         f"{target['running_seconds']}s)"), evidence
            cur.execute("SELECT pg_terminate_backend(%s)", (target["pid"],))
            signalled = cur.fetchone()[0]
            gone, deadline = False, time.monotonic() + settle
            while time.monotonic() < deadline:  # termination is asynchronous
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s",
                            (target["pid"],))
                if cur.fetchone()[0] == 0:
                    gone = True
                    break
                time.sleep(0.25)
            evidence.update({"terminate_returned": signalled, "pid_gone_after": gone,
                             "target_after": find_longest_query(cur)})
            if gone:
                return "executed", f"terminated pid {target['pid']}", evidence
            return "failed", (f"pg_terminate_backend returned {signalled} but pid "
                              f"{target['pid']} still present after {settle}s"), evidence
    except psycopg2.Error as e:
        return "failed", f"{type(e).__name__}: {str(e).strip()}", {}
    finally:
        conn.close()


def reconnect_network(name, dry_run):
    """Re-attach the container to its compose network, but only if it is really detached.
    The network name comes from Docker's own compose labels. Returns (status, reason, evidence)."""
    import docker
    try:
        c = docker.from_env().containers.get(name)
        project = c.labels.get("com.docker.compose.project")
        if not project:
            return "failed", "container has no compose project label; network unknown", {}
        network = f"{project}_default"
        before = sorted(c.attrs["NetworkSettings"]["Networks"])
        evidence = {"network": network, "networks_before": before}
        if network in before:
            return "no_target", f"already connected to {network}", evidence
        if dry_run:
            return "would_execute", f"would connect {name} to {network}", evidence
        # A plain connect loses compose's DNS names, so other services could no longer find
        # this one by name. Restore them: the compose service name + the container name.
        aliases = [a for a in (c.labels.get("com.docker.compose.service"), name) if a]
        docker.from_env().networks.get(network).connect(c, aliases=aliases)
        c.reload()
        after = sorted(c.attrs["NetworkSettings"]["Networks"])
        evidence["networks_after"] = after
        evidence["aliases_after"] = c.attrs["NetworkSettings"]["Networks"].get(
            network, {}).get("Aliases")
        if network in after:
            return "executed", f"connected {name} to {network}", evidence
        return "failed", f"connect returned but {network} not in {after}", evidence
    except Exception as e:  # docker.errors.*, connection errors
        return "failed", f"{type(e).__name__}: {e}", {}


def execute(action, confidence, container, dry_run=True, policy=None, db=None):
    """Returns {action, status, reason, dry_run, ...}. Never raises on policy/Docker problems."""
    out = {"action": action, "container": container, "dry_run": dry_run}
    try:
        policy = policy if policy is not None else load_policy()
    except (OSError, KeyError, TypeError, yaml.YAMLError) as e:
        return {**out, "status": "blocked_pending_human_approval",
                "reason": f"could not load policy.yaml: {e}"}
    allowed, reason = check_policy(action, confidence, policy)
    if not allowed:
        return {**out, "status": "blocked_pending_human_approval", "reason": reason}
    if action == "TERMINATE_QUERY":
        status, why, evidence = terminate_query(db, dry_run)
        return {**out, "status": status, "reason": why, **evidence}
    if action == "RECONNECT_NETWORK":
        status, why, evidence = reconnect_network(container, dry_run)
        return {**out, "status": status, "reason": why, **evidence}
    if dry_run:
        return {**out, "status": "would_execute", "reason": reason}
    if action != "RESTART_CONTAINER":
        return {**out, "status": "not_implemented", "reason": f"{action} has no executor yet"}
    try:
        evidence = restart_container(container)
    except Exception as e:  # docker.errors.*, connection errors
        return {**out, "status": "failed", "reason": f"{type(e).__name__}: {e}"}
    return {**out, "status": "executed", "reason": reason, **evidence}

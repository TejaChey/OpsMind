"""Deterministic verification: did the container come back healthy and serve real queries?

Pure code, no model. Two checks, both must pass inside one overall timeout:
  1. Docker reports the container's health status as "healthy" (polled every `interval` s)
  2. a real Postgres connection succeeds and `SELECT 1` returns 1
With expected_signal="cpu" (used after TERMINATE_QUERY) a third check runs once 1 and 2 pass:
  3. CPU share of the limit, re-sampled over `cpu_window` seconds, is back below
     `cpu_threshold`. If the share can't be measured (no CPU limit, unreadable cgroup) the
     result is verified=False: fail closed.
With expected_signal=None (RESTART_CONTAINER / OOM-style) only checks 1 and 2 run, unchanged.
Never raises: any problem becomes verified=False with a reason.
"""
import sys
import time
from pathlib import Path

import psycopg2

TIMEOUT = 30
INTERVAL = 2
CPU_WINDOW = 5
CPU_THRESHOLD = 0.5   # cpu_share_of_limit must be strictly below this


def _container_health(container):
    """Returns (status, error). status is e.g. 'healthy' / 'starting' / 'unhealthy' / None."""
    try:
        import docker
        c = docker.from_env().containers.get(container)
        state = c.attrs["State"]
        return state.get("Health", {}).get("Status") or state.get("Status"), None
    except Exception as e:  # docker.errors.*, connection errors
        return None, f"{type(e).__name__}: {e}"


def _select_one(host, port, user, password, dbname):
    """Returns (ok, error)."""
    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password,
                                dbname=dbname, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,), None
        finally:
            conn.close()
    except psycopg2.Error as e:
        return False, str(e).strip()


def _cpu_share(container, window):
    """Average cpu_share_of_limit over `window` seconds, using the same cgroup reading as the
    fast tier. Returns (share or None, error or None)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
        import fast_tier
        u0, _, limit, t0 = fast_tier.read_cpu(container)
        time.sleep(window)
        u1, _, limit, t1 = fast_tier.read_cpu(container)
    except Exception as e:  # subprocess/cgroup read problems
        return None, f"{type(e).__name__}: {e}"
    if not limit:
        return None, "container has no CPU limit, so the share cannot be computed"
    return ((u1 - u0) / 1e6 / (t1 - t0)) / limit, None


def verify(container, host, port, user, password, dbname, timeout=TIMEOUT, interval=INTERVAL,
           expected_signal=None, cpu_window=CPU_WINDOW, cpu_threshold=CPU_THRESHOLD):
    """Returns {verified, elapsed_seconds, health_status, reason} (+ cpu_share_after and
    cpu_threshold when expected_signal="cpu")."""
    t0 = time.monotonic()
    health, reason = None, "timed out before container reported health"
    while True:
        health, err = _container_health(container)
        if err:
            reason = f"could not read container health: {err}"
        elif health == "healthy":
            ok, db_err = _select_one(host, port, user, password, dbname)
            if ok and expected_signal == "cpu":
                share, cpu_err = _cpu_share(container, cpu_window)
                result = {"verified": False, "health_status": health,
                          "cpu_share_after": None if share is None else round(share, 3),
                          "cpu_threshold": cpu_threshold}
                if share is None:
                    result["reason"] = f"healthy, but CPU share not measurable: {cpu_err}"
                elif share < cpu_threshold:
                    result.update(verified=True, reason=(
                        f"healthy, SELECT 1 succeeded, CPU share {share:.3f} < {cpu_threshold}"
                        f" over {cpu_window}s"))
                else:
                    result["reason"] = (f"healthy, but CPU share {share:.3f} >= {cpu_threshold}"
                                        f" over {cpu_window}s: CPU did not recover")
                result["elapsed_seconds"] = round(time.monotonic() - t0, 2)
                return result
            if ok:
                return {"verified": True, "elapsed_seconds": round(time.monotonic() - t0, 2),
                        "health_status": health, "reason": "healthy and SELECT 1 succeeded"}
            reason = f"container healthy but SELECT 1 failed: {db_err or 'unexpected result'}"
        else:
            reason = f"container health is {health!r}, not 'healthy'"
        if time.monotonic() - t0 + interval > timeout:
            break
        time.sleep(interval)
    return {"verified": False, "elapsed_seconds": round(time.monotonic() - t0, 2),
            "health_status": health, "reason": f"timed out after {timeout}s: {reason}"}

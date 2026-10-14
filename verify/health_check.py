"""Deterministic verification: did the container come back healthy and serve real queries?

Pure code, no model. Two checks, both must pass inside one overall timeout:
  1. Docker reports the container's health status as "healthy" (polled every `interval` s)
  2. a real Postgres connection succeeds and `SELECT 1` returns 1
With expected_signal="cpu" (used after TERMINATE_QUERY) a third check runs once 1 and 2 pass:
  3. CPU share of the limit, re-sampled over `cpu_window` seconds, is back below
     `cpu_threshold`. If the share can't be measured (no CPU limit, unreadable cgroup) the
     result is verified=False: fail closed.
With expected_signal="disk" (a disk fault was addressed) the third check is instead:
  3. a real write probe (the fast tier's 64 KB INSERT) succeeds AND the data volume is below
     `disk_threshold` full. If either can't be measured the result is verified=False.
With expected_signal="network" (a partition was addressed) the third check is instead:
  3. NET_PROBES real connect + SELECT 1 round trips from the host all succeed and each takes
     less than `latency_threshold` seconds (latency back near baseline, not just "reachable"),
     AND the container has its compose service name as a DNS alias on its network (without
     it, other services cannot find it by name even though the host still can).
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
NET_PROBES = 3
NET_LATENCY_THRESHOLD = 1.0   # seconds per connect+SELECT 1 (healthy baseline is ~0.05s)
DISK_THRESHOLD = 0.90  # data volume usage must be strictly below this (the fast-tier floor is 0.95)


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


def _disk_state(container, host, port, user, password, dbname):
    """(usage fraction or None, write_failed or None, write_error), measured with the same
    functions the fast tier uses to detect the fault."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
    import fast_tier
    usage = fast_tier.read_data_disk(container)
    write_failed, write_error = fast_tier.probe_write(host, port, user, password, dbname)
    return usage, write_failed, write_error


def _service_alias_missing(container):
    """Returns (missing_alias_description or None, error or None). Fails closed on errors."""
    try:
        import docker
        c = docker.from_env().containers.get(container)
        service = c.labels.get("com.docker.compose.service")
        if not service:
            return None, None   # not a compose container: nothing to check
        nets = c.attrs["NetworkSettings"]["Networks"]
        if not any(service in (n.get("Aliases") or []) for n in nets.values()):
            return f"no network has DNS alias {service!r}", None
        return None, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _check_network(host, port, user, password, dbname, threshold, probes=NET_PROBES,
                   container=None):
    """Returns (verified, fields, reason): every probe must succeed and be under threshold,
    and (if `container` is given) the container's compose DNS alias must be present."""
    times = []
    for _ in range(probes):
        t0 = time.monotonic()
        ok, err = _select_one(host, port, user, password, dbname)
        times.append(round(time.monotonic() - t0, 3))
        if not ok:
            return False, {"net_latency_seconds": times, "net_latency_threshold": threshold}, (
                f"probe failed: {err or 'unexpected result'}")
    fields = {"net_latency_seconds": times, "net_latency_threshold": threshold}
    if max(times) >= threshold:
        return False, fields, f"reachable but slow: max {max(times)}s >= {threshold}s"
    if container:
        missing, err = _service_alias_missing(container)
        fields["dns_alias_ok"] = missing is None and err is None
        if err:
            return False, fields, f"could not check DNS alias: {err}"
        if missing:
            return False, fields, f"reachable from host, but {missing}"
    return True, fields, f"{probes} probes succeeded, max {max(times)}s < {threshold}s"


def _check_disk(container, host, port, user, password, dbname, threshold):
    """Returns (verified, fields, reason). Fail closed on anything unmeasured."""
    try:
        usage, write_failed, write_error = _disk_state(container, host, port, user, password,
                                                       dbname)
    except Exception as e:  # subprocess/import problems
        return False, {}, f"disk state not measurable: {type(e).__name__}: {e}"
    fields = {"disk_percent_used_after": usage, "write_failed_after": write_failed,
              "disk_threshold": threshold}
    if write_failed is None:
        return False, fields, f"write probe could not run: {write_error}"
    if write_failed:
        return False, fields, f"write probe still fails: {write_error}"
    if usage is None:
        return False, fields, "write probe ok, but disk usage not measurable"
    if usage >= threshold:
        return False, fields, f"write probe ok, but disk usage {usage} >= {threshold}"
    return True, fields, f"write probe succeeded and disk usage {usage} < {threshold}"


def verify(container, host, port, user, password, dbname, timeout=TIMEOUT, interval=INTERVAL,
           expected_signal=None, cpu_window=CPU_WINDOW, cpu_threshold=CPU_THRESHOLD,
           disk_threshold=DISK_THRESHOLD, latency_threshold=NET_LATENCY_THRESHOLD):
    """Returns {verified, elapsed_seconds, health_status, reason} (+ cpu_share_after and
    cpu_threshold when expected_signal="cpu"; disk_percent_used_after, write_failed_after and
    disk_threshold when expected_signal="disk")."""
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
            if ok and expected_signal == "network":
                good, fields, why = _check_network(host, port, user, password, dbname,
                                                   latency_threshold, container=container)
                return {"verified": good, "health_status": health, **fields,
                        "elapsed_seconds": round(time.monotonic() - t0, 2),
                        "reason": ("healthy, SELECT 1 succeeded, " if good else "healthy, but ")
                        + why}
            if ok and expected_signal == "disk":
                good, fields, why = _check_disk(container, host, port, user, password, dbname,
                                                disk_threshold)
                return {"verified": good, "health_status": health, **fields,
                        "elapsed_seconds": round(time.monotonic() - t0, 2),
                        "reason": ("healthy, SELECT 1 succeeded, " if good else "healthy, but ")
                        + why}
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

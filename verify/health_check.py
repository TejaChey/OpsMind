"""Deterministic verification: did the container come back healthy and serve real queries?

Pure code, no model. Two checks, both must pass inside one overall timeout:
  1. Docker reports the container's health status as "healthy" (polled every `interval` s)
  2. a real Postgres connection succeeds and `SELECT 1` returns 1
With expected_signal="cpu" (used after TERMINATE_QUERY) a third check runs once 1 and 2 pass:
  3. CPU share of the limit, re-sampled over `cpu_window` seconds, is back below
     `cpu_threshold`. A sample that is still too high is re-sampled up to `cpu_resamples` more times
     (the terminated query's tail can still be on the clock), and the LAST sample decides. If the
     share can't be measured (no CPU limit, unreadable cgroup) the result is verified=False: fail closed.
With expected_signal="disk" (a disk fault was addressed) the third check is instead:
  3. a real write probe (the fast tier's 64 KB INSERT) succeeds AND the data volume is below
     `disk_threshold` full. If either can't be measured the result is verified=False.
With expected_signal="network" (a partition was addressed) the third check is instead:
  3. NET_PROBES real connect + SELECT 1 round trips from the host all succeed and each takes
     less than `latency_threshold` seconds (latency back near baseline, not just "reachable"),
     AND the container has its compose service name as a DNS alias on its network (without
     it, other services cannot find it by name even though the host still can).
With expected_signal="app" (a dependency was restarted) the third check is instead:
  3. the dependent web app's own /health returns HTTP 200 with db_ok true. This one is retried
     until the overall timeout, because the app may need a moment to see the database again.
With expected_signal="config" (a config rollback) the app check above must pass AND the live
config must equal the last known-good deploy's config (compared from the files, in code).
With expected_signal=None (RESTART_CONTAINER / OOM-style) only checks 1 and 2 run, unchanged.
Never raises: any problem becomes verified=False with a reason.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg2

TIMEOUT = 30
INTERVAL = 2
CPU_WINDOW = 5
CPU_THRESHOLD = 0.5   # cpu_share_of_limit must be strictly below this
CPU_RESAMPLES = 2     # extra samples allowed ONLY while the last one is still too high (the killed query's tail)
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


def _check_app(app_port):
    """Returns (verified, fields, reason) from a real GET of the app's /health."""
    if not app_port:
        return False, {}, "no app port given, so app health cannot be checked"
    status, body = None, ""
    try:
        with urllib.request.urlopen(f"http://localhost:{app_port}/health", timeout=5) as r:
            status, body = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode()
    except (urllib.error.URLError, OSError) as e:
        return False, {"app_health_status_after": None}, f"app unreachable: {e}"
    try:
        db_ok = json.loads(body).get("db_ok")
    except ValueError:
        db_ok = None
    fields = {"app_health_status_after": status, "app_db_ok_after": db_ok}
    if status == 200 and db_ok is True:
        return True, fields, "app /health is 200 with db_ok true"
    return False, fields, f"app /health is {status} (db_ok={db_ok})"


def _config_matches_known_good():
    """Returns (ok, reason). Fails closed if there is no known-good deploy or on any error."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tools import deploy
        good = deploy.last_good(deploy.read_history())
        if good is None:
            return False, "no known-good deploy to compare the live config with"
        live = deploy.read_live()
        if live != good["config"]:
            return False, f"live config {live} differs from known-good deploy {good['deploy_id']}"
        return True, f"live config equals known-good deploy {good['deploy_id']}"
    except Exception as e:
        return False, f"could not compare configs: {type(e).__name__}: {e}"


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
           disk_threshold=DISK_THRESHOLD, latency_threshold=NET_LATENCY_THRESHOLD,
           app_port=None, cpu_resamples=CPU_RESAMPLES):
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
                samples, cpu_err = [], None
                for _ in range(1 + max(0, cpu_resamples)):
                    share, cpu_err = _cpu_share(container, cpu_window)
                    if share is None:
                        break                      # unmeasurable: fail closed, never retry past it
                    samples.append(round(share, 3))
                    if share < cpu_threshold:
                        break                      # recovered; only a still-high sample is re-sampled
                share = None if cpu_err else samples[-1]   # the verdict is the LAST sample
                result = {"verified": False, "health_status": health,
                          "cpu_share_after": None if share is None else round(share, 3),
                          "cpu_samples": samples, "cpu_threshold": cpu_threshold}
                if share is None:
                    result["reason"] = f"healthy, but CPU share not measurable: {cpu_err}"
                elif share < cpu_threshold:
                    result.update(verified=True, reason=(
                        f"healthy, SELECT 1 succeeded, CPU share {share:.3f} < {cpu_threshold}"
                        f" over {cpu_window}s" + (f" (after {len(samples)} samples {samples})"
                                                   if len(samples) > 1 else "")))
                else:
                    result["reason"] = (f"healthy, but CPU share {share:.3f} >= {cpu_threshold}"
                                        f" over {cpu_window}s: CPU did not recover"
                                        + (f" (samples {samples})" if len(samples) > 1 else ""))
                result["elapsed_seconds"] = round(time.monotonic() - t0, 2)
                return result
            app_fields = {}
            if ok and expected_signal in ("app", "config"):
                good, app_fields, why = _check_app(app_port)
                if good and expected_signal == "config":
                    good, cfg_why = _config_matches_known_good()
                    app_fields["config_matches_known_good"] = good
                    why = f"{why}; {cfg_why}"
                if good:
                    return {"verified": True, "health_status": health, **app_fields,
                            "elapsed_seconds": round(time.monotonic() - t0, 2),
                            "reason": "healthy, SELECT 1 succeeded, " + why}
                reason = f"healthy, but {why}"   # retried below until the timeout
            elif ok and expected_signal == "network":
                good, fields, why = _check_network(host, port, user, password, dbname,
                                                   latency_threshold, container=container)
                return {"verified": good, "health_status": health, **fields,
                        "elapsed_seconds": round(time.monotonic() - t0, 2),
                        "reason": ("healthy, SELECT 1 succeeded, " if good else "healthy, but ")
                        + why}
            elif ok and expected_signal == "disk":
                good, fields, why = _check_disk(container, host, port, user, password, dbname,
                                                disk_threshold)
                return {"verified": good, "health_status": health, **fields,
                        "elapsed_seconds": round(time.monotonic() - t0, 2),
                        "reason": ("healthy, SELECT 1 succeeded, " if good else "healthy, but ")
                        + why}
            elif ok:
                return {"verified": True, "elapsed_seconds": round(time.monotonic() - t0, 2),
                        "health_status": health, "reason": "healthy and SELECT 1 succeeded"}
            else:
                reason = f"container healthy but SELECT 1 failed: {db_err or 'unexpected result'}"
        else:
            reason = f"container health is {health!r}, not 'healthy'"
        if time.monotonic() - t0 + interval > timeout:
            break
        time.sleep(interval)
    return {"verified": False, "elapsed_seconds": round(time.monotonic() - t0, 2),
            "health_status": health, "reason": f"timed out after {timeout}s: {reason}"}

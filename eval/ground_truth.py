"""Independent ground truth for the evaluation: did the system REALLY recover?

These checks are deliberately written separately from verify/health_check.py (the system's own
verifier): they use the Docker CLI instead of the Docker SDK, a direct psycopg2 connection, and
the config files on disk. If the verifier and this module disagree, that disagreement is the
measurement (a verification false-pass or false-fail), so they must not share code.

recovered(fault, ...) -> (True/False, detail), or (None, reason) for faults whose fix is
human-only (OOM, CPU, disk): there the harness cleans up and the system's job is detect + route.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import deploy

AUTO_FIXABLE = {"network-partition", "dependency-down", "config-regression"}
SERVICE_NETWORK = "services_default"


def _docker_json(args):
    r = subprocess.run(["docker", *args], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def container_status(name):
    info = _docker_json(["inspect", name])
    return info[0]["State"]["Status"] if info else None


def app_ok(app_port):
    try:
        with urllib.request.urlopen(f"http://localhost:{app_port}/health", timeout=5) as r:
            return r.status == 200 and json.loads(r.read().decode()).get("db_ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def db_direct_ok(db):
    try:
        conn = psycopg2.connect(connect_timeout=5, **db)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,)
        finally:
            conn.close()
    except psycopg2.Error:
        return False


def _check(fault, container, db, app_port):
    if fault == "network-partition":
        info = _docker_json(["inspect", container])
        net = (info[0]["NetworkSettings"]["Networks"].get(SERVICE_NETWORK) if info else None)
        aliased = bool(net) and "postgres" in (net.get("Aliases") or [])
        return aliased and db_direct_ok(db) and app_ok(app_port), (
            f"attached={bool(net)} alias={aliased}")
    if fault == "dependency-down":
        st = container_status(container)
        return st == "running" and app_ok(app_port), f"container={st}"
    if fault == "config-regression":
        live = deploy.read_live()
        same = live == deploy.GOOD_DEFAULT
        return same and app_ok(app_port), f"live_config_is_baseline={same}"
    return None, "human-only fix: not auto-recoverable"


def recovered(fault, container, db, app_port, wait=10):
    """Poll up to `wait` seconds (state can lag a moment after a fix). (bool|None, detail)."""
    deadline = time.monotonic() + wait
    while True:
        ok, detail = _check(fault, container, db, app_port)
        if ok is None or ok or time.monotonic() >= deadline:
            return ok, detail
        time.sleep(1)

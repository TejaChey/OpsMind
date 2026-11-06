"""Novel faults: failures OpsMind was NOT built for, used to test whether it survives messier,
real-world incidents (eval/novel_faults.py). Each is real (no mocks) and each breaks one
assumption the original 7 faults let us make:

  conn-exhaust  An outside client opens every free Postgres connection and sits idle. The app
                gets "too many clients"; Postgres itself is up and ready. Looks like a network
                problem to the fixed signal. No action on the menu is the right fix (the
                hog's connections must be ended by a person); restarting Postgres "works" but
                kicks every client.
  read-only     Postgres is switched to read-only (ALTER SYSTEM default_transaction_read_only).
                /health (SELECT 1) stays 200, so standard monitoring stays green; writes fail.
                ALTER SYSTEM survives a restart, so a restart does NOT fix it.
  app-paused    The web app container is frozen (`docker pause`): running, but answers nothing.
                The menu has no "unpause"; restarting the database is useless.
  compound      Two faults at once: the cache is stopped AND a bad config is deployed. One
                action can fix at most half of it.

Usage: python faults/novel.py <fault> [--hold | --clear]   (--hold: inject and leave it)
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))
import fast_tier  # noqa: E402
from tools import deploy  # noqa: E402

STATE = ROOT / "faults" / ".novel_state.json"
FAULTS = ("conn-exhaust", "read-only", "app-paused", "compound")


def cfg():
    e = fast_tier.load_env()
    return dict(host="localhost", port=int(e.get("POSTGRES_HOST_PORT", 5433)),
                user=e["POSTGRES_USER"], password=e["POSTGRES_PASSWORD"], dbname=e["POSTGRES_DB"])


def app_port():
    return int(fast_tier.load_env().get("APP_HOST_PORT", 5050))


def psql(*statements):
    """Each statement runs on its own (ALTER SYSTEM refuses to run inside a transaction)."""
    db = cfg()
    cmd = ["docker", "exec", "opsmind-postgres", "psql", "-U", db["user"], "-d", db["dbname"], "-At"]
    for s in statements:
        cmd += ["-c", s]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20)


def save(state):
    STATE.write_text(json.dumps(state))


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


HOG = """
import sys, time, psycopg2
db = dict(host=sys.argv[1], port=int(sys.argv[2]), user=sys.argv[3], password=sys.argv[4],
          dbname=sys.argv[5])
conns = []
while True:
    try:
        conns.append(psycopg2.connect(connect_timeout=3, application_name="batch-exporter", **db))
    except psycopg2.Error:
        break
print(len(conns), flush=True)
while True:
    time.sleep(60)
"""


def inject(fault):
    db = cfg()
    if fault == "conn-exhaust":
        p = subprocess.Popen([sys.executable, "-c", HOG, db["host"], str(db["port"]), db["user"],
                              db["password"], db["dbname"]], stdout=subprocess.PIPE, text=True,
                             start_new_session=True)
        n = p.stdout.readline().strip()
        save({"fault": fault, "hog_pid": p.pid, "connections_held": n})
        return f"hog pid {p.pid} holds {n} idle connections"
    if fault == "read-only":
        out = psql("ALTER SYSTEM SET default_transaction_read_only = on", "SELECT pg_reload_conf()")
        save({"fault": fault})
        return f"read-only on ({out.stdout.strip() or out.stderr.strip()})"
    if fault == "app-paused":
        subprocess.run(["docker", "pause", "opsmind-app"], check=True, capture_output=True)
        save({"fault": fault})
        return "opsmind-app paused"
    if fault == "compound":
        subprocess.run(["docker", "stop", "opsmind-redis"], check=True, capture_output=True)
        deploy.ensure_baseline(app_port())
        good = deploy.last_good(deploy.read_history())["config"]
        e = deploy.deploy({**good, "db_port": 5439}, "bad deploy (port, compound)", app_port(),
                          settle=8)
        save({"fault": fault, "bad_deploy": e["deploy_id"]})
        return f"redis stopped AND bad deploy {e['deploy_id']} (status {e['status']})"
    raise SystemExit(f"unknown fault {fault}")


def clear(fault):
    st = load()
    if fault == "conn-exhaust" and st.get("hog_pid"):
        try:
            os.killpg(st["hog_pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    if fault == "read-only":
        psql("ALTER SYSTEM RESET default_transaction_read_only", "SELECT pg_reload_conf()")
    if fault == "app-paused":
        subprocess.run(["docker", "unpause", "opsmind-app"], capture_output=True)
    if fault == "compound":
        subprocess.run(["docker", "start", "opsmind-redis"], capture_output=True)
        tgt, _ = deploy.rollback_target()
        if tgt:
            deploy.deploy(tgt["config"], f"manual clear (restore {tgt['deploy_id']})", app_port(),
                          kind="rollback", restores=tgt["deploy_id"], settle=10)
    STATE.unlink(missing_ok=True)
    for _ in range(30):     # wait until the system is healthy again
        status, _, _ = fast_tier.probe_app(app_port(), timeout=3)
        if status == 200 and fault_present(fault) is False:
            return "cleared, app healthy"
        time.sleep(2)
    return "cleared, but app not healthy yet"


def fault_present(fault):
    """Independent check (not the agent's verifier) that the fault is really in effect."""
    if fault == "conn-exhaust":
        out = psql("SELECT count(*) FROM pg_stat_activity WHERE application_name='batch-exporter'")
        if out.returncode != 0:     # psql itself was refused: no connection slots left
            return "too many clients" in out.stderr or "remaining connection slots" in out.stderr
        return int(out.stdout.strip() or 0) > 50
    if fault == "read-only":
        return psql("SHOW default_transaction_read_only").stdout.strip() == "on"
    if fault == "app-paused":
        return fast_tier.container_state("opsmind-app") == "paused"
    if fault == "compound":
        live = deploy.read_live()
        good = deploy.last_good(deploy.read_history())
        return fast_tier.container_state("opsmind-redis") != "running" \
            and good is not None and live != good["config"]
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("fault", choices=FAULTS)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--hold", action="store_true")
    g.add_argument("--clear", action="store_true")
    a = p.parse_args()
    if a.clear:
        print(clear(a.fault))
        return
    print(inject(a.fault))
    time.sleep(3)
    print(f"fault present (independent check): {fault_present(a.fault)}")
    if not a.hold:
        print(clear(a.fault))


if __name__ == "__main__":
    main()

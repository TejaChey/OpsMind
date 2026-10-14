"""Fault: config regression after deploy. Deploys a BAD config to the web app through the same
deploy tool a normal deploy uses (tools/deploy.py), so it lands in deploys/history.jsonl. The
app re-reads its config, so it really starts failing, while Postgres stays perfectly healthy.

Usage:  python faults/config_regression.py [--variant port|host] [--app-port P]
                                           [--dependency NAME] [--hold | --clear]

Variants (what the bad deploy changes):
  port  db_port -> 5439   the app is refused at a port nothing listens on ("connection refused")
  host  db_host -> a typo  the app cannot resolve the database's name
--hold leaves the bad deploy live; --clear rolls back by hand (a manual deploy of the last
known-good config, recorded in the history); with neither, the script rolls back at the end.

Prints FAULT TOOK EFFECT only if ALL are measured true: the app's /health was 200 before and
is 503 after; the Postgres container is still running and a direct connection from the host
still works (so the database is NOT the problem); and the bad deploy was recorded in the
history with status "bad". No agent logic.
"""
import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import deploy

ENV_FILE = ROOT / "services" / ".env"
VARIANTS = {"port": {"db_port": 5439}, "host": {"db_host": "postgress"}}


def load_env(path=ENV_FILE):
    cfg = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    cfg.update({k: v for k, v in os.environ.items() if k.startswith(("POSTGRES_", "APP_"))})
    return cfg


def app_status(port):
    """(http status or None, body/error text)."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5) as r:
            return r.status, r.read().decode().strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode().strip()
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def db_direct_ok(cfg):
    """Real connect + SELECT 1 straight to Postgres from the host, bypassing the app."""
    try:
        conn = psycopg2.connect(host="localhost", port=int(cfg["POSTGRES_HOST_PORT"]),
                                user=cfg["POSTGRES_USER"], password=cfg["POSTGRES_PASSWORD"],
                                dbname=cfg["POSTGRES_DB"], connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,)
        finally:
            conn.close()
    except psycopg2.Error:
        return False


def manual_rollback(app_port):
    """What a human would do: deploy the last known-good config again."""
    good = deploy.last_good(deploy.read_history())
    if good is None:
        sys.exit("no known-good deploy to roll back to")
    if deploy.read_live() == good["config"]:
        print(f"already on known-good deploy {good['deploy_id']}")
        return
    entry = deploy.deploy(good["config"], f"manual rollback to {good['deploy_id']}", app_port,
                          kind="rollback", restores=good["deploy_id"])
    print(f"rolled back to {good['deploy_id']} -> new entry {entry['deploy_id']} "
          f"status={entry['status']}")


def main():
    cfg = load_env()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--variant", choices=sorted(VARIANTS), default="port")
    p.add_argument("--app-port", type=int, default=int(cfg.get("APP_HOST_PORT", 5050)))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--hold", action="store_true", help="leave the bad deploy live")
    g.add_argument("--clear", action="store_true", help="only roll back by hand, then exit")
    args = p.parse_args()
    if args.clear:
        return manual_rollback(args.app_port)

    base = deploy.ensure_baseline(args.app_port)
    if base:
        print(f"no known-good deploy yet: deployed baseline {base['deploy_id']} "
              f"(status {base['status']})")
    if deploy.read_live() != deploy.last_good(deploy.read_history())["config"]:
        sys.exit("live config is not the known-good one; run with --clear first")
    st_b, body_b = app_status(args.app_port)
    print(f"before: app /health -> {st_b} {body_b}")
    if st_b != 200:
        sys.exit("app is not healthy even before the fault; fix that first")

    bad = {**deploy.read_live(), **VARIANTS[args.variant]}
    entry = deploy.deploy(bad, f"bad deploy ({args.variant})", args.app_port, settle=8)
    print(f"deployed {entry['deploy_id']}: changed {deploy.diff(deploy.last_good(deploy.read_history()[:-1])['config'], bad)}")
    st_a, body_a = app_status(args.app_port)
    db_ok = db_direct_ok(cfg)
    print(f"after:  app /health -> {st_a} {body_a}")
    print(f"after:  direct database connection from the host works: {db_ok}")
    print(f"after:  history says deploy {entry['deploy_id']} status={entry['status']} "
          f"({entry['health_after']})")

    if args.hold:
        print("hold: bad deploy left live; run with --clear to roll back")
    else:
        manual_rollback(args.app_port)

    if st_b == 200 and st_a == 503 and db_ok and entry["status"] == "bad":
        print("FAULT TOOK EFFECT (app /health 200 -> 503 after a deploy; database itself is fine)")
    else:
        why = []
        if st_a != 503:
            why.append(f"app /health after deploy was {st_a}, not 503")
        if not db_ok:
            why.append("database itself not reachable (that would be a different fault)")
        if entry["status"] != "bad":
            why.append(f"deploy recorded as {entry['status']}")
        print("NO CONFIG-REGRESSION FAULT OBSERVED: " + "; ".join(why))


if __name__ == "__main__":
    main()

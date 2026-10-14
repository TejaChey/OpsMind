"""Fault: disk full. Fills Postgres's (small, size-capped) data volume to near-full with a real
dummy file, then attempts a real INSERT and checks that Postgres genuinely fails with
"No space left on device". Nothing is simulated: the cap is a real tmpfs mount
(see `volumes: pgdata` in services/docker-compose.yml) and the error comes from the kernel.

Usage:  python faults/disk_full.py [--container NAME] [--host H] [--port P]
                                   [--headroom-mb N] [--rows N] [--recover-timeout S]
                                   [--hold | --clear]

--hold leaves the volume full (filler file kept) so an agent can observe the live fault;
--clear is the matching manual cleanup (removes the filler, test table and write-probe table).

Samples usage of the data mount (df, inside the container) before the fault, after the fill,
after the write attempt, and after cleanup. Prints FAULT TOOK EFFECT only if the mount was
filled to >= 95% AND a real write failed with ENOSPC (either the client got the error, or the
connection died and the server log shows it). Always removes the dummy file and test table
afterwards. No agent logic.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

ENV_FILE = Path(__file__).resolve().parent.parent / "services" / ".env"
DATA_DIR = "/var/lib/postgresql/data"
FILLER = f"{DATA_DIR}/opsmind_disk_filler"
TABLE = "disk_fault_test"
NOSPACE = "no space left on device"
FILL_THRESHOLD = 0.95


def load_env(path=ENV_FILE):
    """Settings from services/.env (the file docker-compose uses); real env vars win."""
    cfg = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("POSTGRES_")})
    return cfg


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def inspect(container):
    return json.loads(docker("inspect", container).stdout)[0]


def mount_usage(container):
    """Size/used/avail of the data mount, measured by df inside the container."""
    out = docker("exec", container, "df", "-B1", "--output=size,used,avail", DATA_DIR).stdout
    size, used, avail = (int(x) for x in out.splitlines()[-1].split())
    return {"size": size, "used": used, "avail": avail, "pct": used / size}


def show(label, u):
    print(f"  [{label}] {u['used'] / 2**20:6.1f} / {u['size'] / 2**20:.1f} MiB used "
          f"= {u['pct'] * 100:5.1f}%  ({u['avail'] / 2**20:.1f} MiB free)")


def connect(args, timeout=5):
    conn = psycopg2.connect(host=args.host, port=args.port, user=args.user,
                            password=args.password, dbname=args.dbname, connect_timeout=timeout)
    conn.autocommit = True
    return conn


def wait_for_db(args, seconds):
    """Reconnect loop; returns a connection or None."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            return connect(args)
        except psycopg2.Error:
            time.sleep(2)
    return None


def attempt_write(args, rows):
    """Try the big INSERT. Returns (kind, detail): kind is 'enospc', 'conn_lost',
    'other_error' or 'succeeded'."""
    conn = connect(args)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 30000")
            cur.execute(f"INSERT INTO {TABLE}(payload) "
                        f"SELECT repeat('x', 1000000) FROM generate_series(1, {rows})")
        return "succeeded", f"inserted {rows} MB without error"
    except psycopg2.errors.DiskFull as e:
        return "enospc", str(e).strip()
    except psycopg2.Error as e:
        msg = str(e).strip()
        if NOSPACE in msg.lower():
            return "enospc", msg
        if isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            return "conn_lost", msg  # e.g. server PANIC on a WAL write -> connection dropped
        return "other_error", msg
    finally:
        try:
            conn.close()
        except Exception:
            pass


PROBE_TABLE = "opsmind_write_probe"   # created by the fast tier's write probe


def clear(args):
    """Manual cleanup after --hold: remove the filler file, then drop the leftover tables."""
    docker("exec", "-u", "postgres", args.container, "rm", "-f", FILLER)
    conn = wait_for_db(args, args.recover_timeout)
    if conn is None:
        sys.exit(f"filler removed, but Postgres not accepting connections after {args.recover_timeout}s")
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
    conn.close()
    show("after clear", mount_usage(args.container))
    print("cleared: filler file removed, test + write-probe tables dropped")


def main():
    cfg = load_env()
    missing = [k for k in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
                           "POSTGRES_HOST_PORT") if k not in cfg]
    if missing:
        sys.exit(f"missing {missing}: set them in {ENV_FILE} or the environment")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--container", default="opsmind-postgres")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=int(cfg["POSTGRES_HOST_PORT"]))
    p.add_argument("--user", default=cfg["POSTGRES_USER"])
    p.add_argument("--password", default=cfg["POSTGRES_PASSWORD"])
    p.add_argument("--dbname", default=cfg["POSTGRES_DB"])
    p.add_argument("--headroom-mb", type=int, default=2,
                   help="MiB left free after the fill (default 2)")
    p.add_argument("--rows", type=int, default=40,
                   help="number of 1 MB rows the failing INSERT tries to write")
    p.add_argument("--recover-timeout", type=int, default=60,
                   help="seconds to wait for Postgres to accept connections after cleanup")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--hold", action="store_true",
                   help="leave the volume full after the fault (clear it with --clear)")
    g.add_argument("--clear", action="store_true",
                   help="only clean up a previous --hold run, then exit")
    args = p.parse_args()
    if args.clear:
        return clear(args)

    info = inspect(args.container)
    mount = next((m for m in info["Mounts"] if m["Destination"] == DATA_DIR), None)
    if mount is None:
        sys.exit(f"{DATA_DIR} is not a separate mount; see `volumes:` in docker-compose.yml")
    before = mount_usage(args.container)
    if before["size"] > 1024 * 2**20:
        sys.exit(f"data mount is {before['size'] // 2**20} MiB: not a small capped volume")
    restarts_before = info["RestartCount"]
    print(f"data mount: volume={mount['Name']} cap={before['size'] // 2**20} MiB "
          f"(df measured inside {args.container})")

    try:
        conn = connect(args)
    except psycopg2.Error as e:
        sys.exit(f"cannot connect to Postgres at {args.host}:{args.port}: {e}")
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(f"CREATE TABLE {TABLE}(id serial PRIMARY KEY, payload text)")
        cur.execute(f"ALTER TABLE {TABLE} ALTER COLUMN payload SET STORAGE EXTERNAL")  # no compression
        cur.execute(f"INSERT INTO {TABLE}(payload) VALUES ('control row before the fault')")
    conn.close()
    print("control INSERT before the fault: OK")
    print("disk usage:")
    show("before", before)

    kind, detail, filled, after_write = "not_attempted", "", None, None
    t_attempt = None
    try:
        free = mount_usage(args.container)["avail"]
        fill_bytes = free - args.headroom_mb * 2**20
        if fill_bytes <= 0:
            sys.exit("not enough free space to fill; reset the volume (compose down -v)")
        docker("exec", "-u", "postgres", args.container, "fallocate", "-l",
               str(fill_bytes), FILLER)
        filled = mount_usage(args.container)
        show("filled", filled)

        t_attempt = datetime.now(timezone.utc).isoformat()
        print(f"attempting INSERT of {args.rows} x 1 MB rows with "
              f"{filled['avail'] / 2**20:.1f} MiB free...")
        kind, detail = attempt_write(args, args.rows)
        print(f"write attempt result: {kind}: {detail[:300]}")
        try:
            after_write = mount_usage(args.container)
            show("after write attempt", after_write)
        except subprocess.CalledProcessError:
            print("  [after write attempt] container not exec-able right now")
    finally:
        if args.hold:
            print("hold: leaving the dummy file in place (volume stays full); "
                  "run with --clear to clean up")
        else:
            print("cleanup: removing dummy file and test table")
            docker("exec", "-u", "postgres", args.container, "rm", "-f", FILLER, check=False)

    log_hit = False
    if t_attempt:
        logs = docker("logs", "--since", t_attempt, args.container, check=False)
        log_hit = NOSPACE in (logs.stdout + logs.stderr).lower()

    conn = wait_for_db(args, args.recover_timeout)
    if conn is None:
        print(f"WARNING: Postgres not accepting connections {args.recover_timeout}s after cleanup")
    elif not args.hold:  # held runs keep the tables; --clear drops them
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
            cur.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        conn.close()
    else:
        conn.close()
    after = mount_usage(args.container)
    show("after cleanup", after)
    info = inspect(args.container)
    print(f"container after: status={info['State']['Status']} "
          f"health={info['State'].get('Health', {}).get('Status', 'n/a')} "
          f"restarts {restarts_before} -> {info['RestartCount']}  "
          f"server log mentions ENOSPC during attempt: {log_hit}")

    real_failure = kind == "enospc" or (kind == "conn_lost" and log_hit)
    full_enough = filled is not None and filled["pct"] >= FILL_THRESHOLD
    if real_failure and full_enough:
        how = "client received the ENOSPC error" if kind == "enospc" else \
            "connection dropped and the server log shows ENOSPC"
        print(f"FAULT TOOK EFFECT ({how}; volume {filled['pct'] * 100:.1f}% full)")
    else:
        why = []
        if not full_enough:
            why.append("volume not filled to 95%")
        if not real_failure:
            why.append(f"no real ENOSPC write failure (result: {kind})")
        print("NO DISK-FULL FAULT OBSERVED: " + "; ".join(why))


if __name__ == "__main__":
    main()

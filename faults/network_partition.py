"""Fault: network partition. Really disconnects the Postgres container from its Docker network
(`docker network disconnect`), so the host can no longer reach it, while the Postgres process
itself keeps running. Nothing is simulated: the host's connection attempt genuinely times out.

Usage:  python faults/network_partition.py [--container NAME] [--host H] [--port P]
                                           [--network NAME] [--probe-timeout S] [--hold | --clear]

--hold leaves the partition in place so an agent can observe it; --clear reconnects (the
matching manual cleanup). Without either, the script reconnects at the end.

Prints FAULT TOOK EFFECT only if ALL of these are measured true: a connection from the host
worked before; it failed after the disconnect; the container is still running; and
pg_isready run INSIDE the container still says Postgres is accepting connections (so the
process is fine and the network is the problem). No agent logic.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

ENV_FILE = Path(__file__).resolve().parent.parent / "services" / ".env"


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


def networks(container):
    info = json.loads(docker("inspect", container).stdout)[0]
    return info["State"]["Status"], sorted(info["NetworkSettings"]["Networks"])


def host_probe(args, timeout):
    """Real connect + SELECT 1 from the host. Returns (ok, seconds, error)."""
    t0 = time.monotonic()
    try:
        conn = psycopg2.connect(host=args.host, port=args.port, user=args.user,
                                password=args.password, dbname=args.dbname,
                                connect_timeout=timeout)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return True, round(time.monotonic() - t0, 3), None
    except psycopg2.Error as e:
        return False, round(time.monotonic() - t0, 3), str(e).strip().splitlines()[0]


def inside_ready(container, user, dbname):
    """pg_isready run inside the container (over its local socket, no network involved)."""
    r = docker("exec", container, "pg_isready", "-U", user, "-d", dbname, check=False)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def reconnect(args):
    status, nets = networks(args.container)
    if args.network in nets:
        print(f"already connected to {args.network}")
        return
    # Re-add compose's DNS names too (a plain connect loses them, and other services then
    # cannot find this container by name).
    labels = json.loads(docker("inspect", args.container).stdout)[0]["Config"]["Labels"]
    aliases = [a for a in (labels.get("com.docker.compose.service"), args.container) if a]
    cmd = ["network", "connect"] + [x for a in aliases for x in ("--alias", a)]
    docker(*cmd, args.network, args.container)
    print(f"reconnected {args.container} to {args.network} (aliases {aliases})")


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
    p.add_argument("--network", default="services_default")
    p.add_argument("--probe-timeout", type=int, default=3,
                   help="seconds the host waits before calling the connection failed")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--hold", action="store_true", help="leave the partition in place")
    g.add_argument("--clear", action="store_true", help="only reconnect, then exit")
    args = p.parse_args()
    if args.clear:
        return reconnect(args)

    status, nets = networks(args.container)
    if args.network not in nets:
        sys.exit(f"{args.container} is not on {args.network} (networks: {nets}); "
                 "run with --clear first")
    ok_b, sec_b, err_b = host_probe(args, args.probe_timeout)
    print(f"before: container={status} networks={nets}")
    print(f"before: host connection ok={ok_b} in {sec_b}s {err_b or ''}")
    if not ok_b:
        sys.exit("host cannot connect even before the fault; fix that first")

    docker("network", "disconnect", args.network, args.container)
    try:
        status_a, nets_a = networks(args.container)
        ok_a, sec_a, err_a = host_probe(args, args.probe_timeout)
        ready, ready_out = inside_ready(args.container, args.user, args.dbname)
        print(f"after:  container={status_a} networks={nets_a}")
        print(f"after:  host connection ok={ok_a} in {sec_a}s  error={err_a}")
        print(f"after:  pg_isready inside container: {ready} ({ready_out})")
    finally:
        if args.hold:
            print("hold: partition left in place; run with --clear to reconnect")
        else:
            reconnect(args)

    if not args.hold:
        ok_r, sec_r, _ = host_probe(args, 15)
        print(f"after reconnect: host connection ok={ok_r} in {sec_r}s")

    if ok_b and not ok_a and status_a == "running" and ready:
        print("FAULT TOOK EFFECT (host cannot connect; Postgres process is up and ready inside)")
    else:
        why = []
        if ok_a:
            why.append("host connection still worked")
        if status_a != "running":
            why.append(f"container is {status_a}, not running")
        if not ready:
            why.append("Postgres not ready inside the container")
        print("NO NETWORK-PARTITION FAULT OBSERVED: " + "; ".join(why))


if __name__ == "__main__":
    main()

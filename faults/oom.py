"""Fault: OOM. Runs a memory-exhausting query inside Postgres so a backend process
exceeds the container's cgroup memory limit and the kernel OOM killer fires.

Usage:  python faults/oom.py [--container NAME] [--host H] [--port P] [--timeout SECONDS]
                             [--settle SECONDS] [--reset]

Prints a before/after report (OOMKilled, RestartCount, health, cgroup oom_kill count).
No agent logic.

Note: the OOM killer normally kills the Postgres *backend*, not PID 1, so the postmaster
survives and does crash recovery. Docker's OOMKilled flag therefore usually stays false;
the cgroup memory.events `oom_kill` counter is the authoritative signal.
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

# ~2 TB of 1 MB strings: can never fit, so the backend grows until the cgroup limit hits.
OOM_QUERY = "SELECT array_agg(repeat('x', 1000000)) FROM generate_series(1, 2000000)"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def inspect(container):
    return json.loads(docker("inspect", container).stdout)[0]


def snapshot(container):
    info = inspect(container)
    state = info["State"]
    return {
        "status": state["Status"],
        "health": state.get("Health", {}).get("Status", "n/a"),
        "oom_killed": state["OOMKilled"],
        "restart_count": info["RestartCount"],
        "pid": state["Pid"],
    }


def oom_events(container):
    """Kernel-level OOM kills seen by the cgroup (works even when PID 1 survives)."""
    r = docker("exec", container, "sh", "-c",
               "cat /sys/fs/cgroup/memory.events 2>/dev/null || "
               "cat /sys/fs/cgroup/memory/memory.oom_control 2>/dev/null", check=False)
    return r.stdout.strip()


def oom_kill_count(events):
    for line in events.splitlines():
        key, _, val = line.partition(" ")
        if key == "oom_kill":
            return int(val)
    return None


def reset_container(container, wait=90):
    """Recreate the stack from scratch and block until the container is healthy."""
    compose = ["compose", "-f", str(ENV_FILE.parent / "docker-compose.yml")]
    print("reset: docker compose down -v && up -d --build")
    docker(*compose, "down", "-v")
    docker(*compose, "up", "-d", "--build")
    deadline = time.time() + wait
    while time.time() < deadline:
        if snapshot(container)["health"] == "healthy":
            return
        time.sleep(2)
    sys.exit(f"reset: {container} not healthy after {wait}s")


def run_oom_query(args):
    """Returns a string describing how the query ended."""
    try:
        conn = psycopg2.connect(host=args.host, port=args.port, user=args.user,
                                password=args.password, dbname=args.dbname,
                                connect_timeout=5)
    except psycopg2.OperationalError as e:
        sys.exit(f"cannot connect to Postgres at {args.host}:{args.port}: {e}")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {args.timeout * 1000}")
            cur.execute(OOM_QUERY)
            return "query completed (unexpected: no OOM)"
    except psycopg2.errors.QueryCanceled:
        return f"query hit statement_timeout ({args.timeout}s) without being killed"
    except psycopg2.errors.OutOfMemory as e:
        return f"Postgres reported out of memory (not a kernel kill): {e}".strip()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        # Backend killed -> "server closed the connection unexpectedly" / crash recovery.
        return f"connection lost (backend killed): {str(e).strip()}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    p.add_argument("--timeout", type=int, default=60,
                   help="seconds before the query is abandoned if nothing kills it")
    p.add_argument("--settle", type=int, default=15,
                   help="seconds to watch container health after the fault")
    p.add_argument("--reset", action="store_true",
                   help="docker compose down -v && up -d --build first (wipes the DB volume) "
                        "so OOMKilled and cgroup counters start clean")
    args = p.parse_args()

    if args.reset:
        reset_container(args.container)

    limit = inspect(args.container)["HostConfig"]["Memory"]
    if not limit:
        sys.exit("container has no memory limit; set mem_limit in docker-compose.yml")

    print(f"limit={limit // 2**20}MiB  query={OOM_QUERY}")
    print("before:", snapshot(args.container))
    events_before = oom_events(args.container)
    print("cgroup memory events before:\n" + events_before)

    outcome = run_oom_query(args)
    print("outcome:", outcome)

    # Health flaps during crash recovery; sample it rather than trusting one reading.
    print(f"health after fault (sampling {args.settle}s):")
    deadline = time.time() + args.settle
    try:
        while time.time() < deadline:
            s = snapshot(args.container)
            print(f"  t+{args.settle - int(deadline - time.time())}s", s)
            time.sleep(3)
        print("after:", snapshot(args.container))
        events_after = oom_events(args.container)
        print("cgroup memory events after:\n" + events_after)
    except subprocess.CalledProcessError as e:
        print("container not inspectable/exec-able after fault:", e)
        return

    b, a = oom_kill_count(events_before), oom_kill_count(events_after)
    if b is not None and a is not None:
        print(f"oom_kill delta: {a - b}  ->  "
              f"{'FAULT TOOK EFFECT' if a > b else 'NO OOM KILL OBSERVED'}")


if __name__ == "__main__":
    main()

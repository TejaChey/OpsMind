"""Fault: CPU spike. Runs a genuinely expensive query inside Postgres (an unindexed cross join
that md5-hashes ~400M generated rows) so the Postgres process itself burns CPU until the
container's cgroup CPU limit throttles it. No external stress tool.

Usage:  python faults/cpu_spike.py [--container NAME] [--host H] [--port P]
                                   [--duration SECONDS] [--baseline SECONDS]

Samples the container cgroup's cpu.stat (usage_usec, nr_throttled, throttled_usec) every
second: first an idle baseline, then during the query. Prints average CPU (in cores)
against the configured limit. The query is cancelled after --duration (that is the expected
ending, not a failure). No agent logic.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg2

ENV_FILE = Path(__file__).resolve().parent.parent / "services" / ".env"

# 20000 x 20000 = 400M md5 calls with no index or join key: far more than can finish in
# --duration at a 0.5-CPU limit, so the backend stays CPU-bound the whole window.
CPU_QUERY = ("SELECT sum(length(md5(a::text || b::text))) "
             "FROM generate_series(1, 20000) a, generate_series(1, 20000) b")


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


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True)


def cpu_limit_cores(container):
    nano = json.loads(docker("inspect", container).stdout)[0]["HostConfig"]["NanoCpus"]
    return nano / 1e9 if nano else None


def read_cpu_stat(container):
    """cgroup v2 cpu.stat as a dict, plus the monotonic time it was read."""
    out = docker("exec", container, "cat", "/sys/fs/cgroup/cpu.stat").stdout
    t = time.monotonic()
    stat = {k: int(v) for k, v in (line.split() for line in out.splitlines())}
    return t, stat


def run_query(args, result):
    try:
        conn = psycopg2.connect(host=args.host, port=args.port, user=args.user,
                                password=args.password, dbname=args.dbname, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(args.duration * 1000)}")
            try:
                cur.execute(CPU_QUERY)
                result["outcome"] = "query completed before the duration (unexpectedly cheap)"
            except psycopg2.errors.QueryCanceled:
                result["outcome"] = f"query cancelled at statement_timeout ({args.duration}s) (expected)"
        conn.close()
    except psycopg2.Error as e:
        result["outcome"] = f"query error: {str(e).strip()}"


def sample(container, seconds, label):
    """Read cpu.stat once a second for `seconds`; returns list of (t, stat)."""
    samples = [read_cpu_stat(container)]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(1)
        samples.append(read_cpu_stat(container))
        (t0, s0), (t1, s1) = samples[-2], samples[-1]
        cores = (s1["usage_usec"] - s0["usage_usec"]) / 1e6 / (t1 - t0)
        print(f"  [{label}] {cores:5.2f} cores  throttled_periods+="
              f"{s1['nr_throttled'] - s0['nr_throttled']}")
    return samples


def summarize(samples, limit):
    (t0, s0), (t1, s1) = samples[0], samples[-1]
    wall = t1 - t0
    cores = (s1["usage_usec"] - s0["usage_usec"]) / 1e6 / wall
    return {"wall_s": wall, "cpu_s": (s1["usage_usec"] - s0["usage_usec"]) / 1e6,
            "avg_cores": cores, "pct_of_limit": cores / limit * 100 if limit else None,
            "nr_throttled": s1["nr_throttled"] - s0["nr_throttled"],
            "throttled_s": (s1["throttled_usec"] - s0["throttled_usec"]) / 1e6}


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
    p.add_argument("--duration", type=int, default=20, help="seconds the query runs")
    p.add_argument("--baseline", type=int, default=5, help="idle seconds sampled first")
    args = p.parse_args()

    limit = cpu_limit_cores(args.container)
    if not limit:
        sys.exit("container has no CPU limit; set cpus in docker-compose.yml "
                 "and run `docker compose -f services/docker-compose.yml up -d`")
    print(f"limit={limit} cores  query={CPU_QUERY}")

    print(f"baseline (idle, {args.baseline}s):")
    base = summarize(sample(args.container, args.baseline, "idle"), limit)

    result = {}
    th = threading.Thread(target=run_query, args=(args, result))
    print(f"fault (query runs up to {args.duration}s):")
    th.start()
    time.sleep(1)  # let the backend start before the first sample
    fault_samples = sample(args.container, args.duration - 2, "fault")
    th.join(timeout=args.duration + 15)
    print("outcome:", result.get("outcome", "query thread did not finish"))

    f = summarize(fault_samples, limit)
    print(f"\nidle : avg {base['avg_cores']:.3f} cores ({base['pct_of_limit']:.1f}% of limit)")
    print(f"fault: avg {f['avg_cores']:.3f} cores = {f['pct_of_limit']:.1f}% of the "
          f"{limit}-core limit over {f['wall_s']:.1f}s ({f['cpu_s']:.1f} CPU-seconds)")
    print(f"       throttled in {f['nr_throttled']} scheduler periods, "
          f"{f['throttled_s']:.1f}s total throttled time")
    hit = f["pct_of_limit"] >= 80 and f["nr_throttled"] > 0
    print("verdict:", "FAULT TOOK EFFECT (CPU at/near limit and throttled)" if hit
          else "NO CPU SPIKE OBSERVED")


if __name__ == "__main__":
    main()

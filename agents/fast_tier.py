"""Fast decision tier: classify one incident signal -> {severity, escalate, confidence}.

The decision is driven by two cgroup readings taken before and after a sampling window --
the `oom_kill` delta (memory.events) and CPU use vs. the limit (cpu.stat, cpu.max) -- plus
disk state (data_disk_percent_used from df, write_failed from a real INSERT probe), plus
whether a DB connection succeeds. One classifier handles all fault types; signals without the
CPU or disk keys (older records) still work. Container health and Docker's
OOMKilled flag are deliberately NOT inputs: both can read "healthy"/stale during a real
incident. The model's JSON is validated in code; on malformed output we retry once, then
fall back to escalate=true. A deterministic floor then overrides the model and forces
escalate=true and severity >= 0.8 when EITHER any oom_kill delta > 0 (local models
under-escalated a real OOM kill) OR cpu_share_of_limit >= 0.8 AND cpu_throttled_periods > 0
OR write_failed is True OR data_disk_percent_used >= 0.95 OR (db_connect_ok is False AND
pg_ready_inside is True: the host cannot connect but Postgres is ready inside its own
container, i.e. a network partition).

Usage:  python agents/fast_tier.py [--window SECONDS] [--model NAME] [--container NAME]
        (run it, then trigger `python faults/oom.py` or `python faults/cpu_spike.py` from
        another shell inside the window)
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import requests

ENV_FILE = Path(__file__).resolve().parent.parent / "services" / ".env"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OOM_SEVERITY_FLOOR = 0.8
CPU_SHARE_THRESHOLD = 0.8   # share of the CPU limit that, with throttling, counts as a spike
CPU_SEVERITY_FLOOR = 0.8
DATA_DIR = "/var/lib/postgresql/data"   # the capped data volume (see docker-compose.yml)
DISK_USED_THRESHOLD = 0.95              # fraction of the data volume in use
DISK_SEVERITY_FLOOR = 0.8
NET_SEVERITY_FLOOR = 0.8
PROBE_TABLE = "opsmind_write_probe"
PROBE_BYTES = 64 * 1024   # big enough to force a real file extension, small enough to be harmless
DEFAULT_MODEL = os.environ.get("OPSMIND_FAST_MODEL", "qwen2.5:3b")

# Strict schema: exactly these keys, these types. Also sent to Ollama as the output format.
SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "number", "minimum": 0, "maximum": 1},
        "escalate": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["severity", "escalate", "confidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You triage incident signals for a containerized Postgres database.
You are given facts measured by code. Do not invent facts. Reply with JSON only:
{"severity": <number 0-1>, "escalate": <true|false>, "confidence": <number 0-1>}
Guidance:
- oom_kill_delta is how many processes the kernel killed for exceeding the memory limit
  during the sampling window. Any value above 0 is a real memory incident.
- cpu_share_of_limit is the average CPU use during the window as a fraction of the container's
  CPU limit (1.0 = pinned at the limit); cpu_throttled_periods is how many scheduler periods
  the kernel throttled the container. High share with throttling is a real CPU incident.
  These two keys can be absent or null when not measured.
- data_disk_percent_used is the fraction (0-1) of the database's data volume in use;
  write_failed=true means a real INSERT just failed (write_error says why, e.g. "No space left
  on device"). A nearly full volume or a failed write is a real disk incident.
- db_connect_ok=false means clients cannot reach the database right now.
  pg_ready_inside=true means Postgres reports itself ready when asked from inside its own
  container, so db_connect_ok=false with pg_ready_inside=true points at the network, not at
  the database process. db_connect_seconds is how long the host's connection attempt took.
  These two keys can be absent or null when not measured.
- oom_kill_delta 0, no CPU pressure and db_connect_ok true means no incident: low severity,
  escalate false.
- Higher severity when oom_kill_delta is above 0, CPU is pinned and throttled, the data volume
  is nearly full or a write failed; higher still when the connection also fails.
- escalate=true when the signal indicates an incident that needs remediation.
- confidence is how sure you are of this classification."""


# ---------- signal collection (all deterministic, no model) ----------

def load_env(path=ENV_FILE):
    """Settings from services/.env (same file docker-compose uses); real env vars win."""
    cfg = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("POSTGRES_")})
    return cfg


def read_oom_kill(container):
    """Current cumulative cgroup oom_kill counter, or raise if it can't be read."""
    r = subprocess.run(
        ["docker", "exec", container, "cat", "/sys/fs/cgroup/memory.events"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cannot read memory.events from {container}: {r.stderr.strip()}")
    for line in r.stdout.splitlines():
        key, _, val = line.partition(" ")
        if key == "oom_kill":
            return int(val)
    raise RuntimeError(f"no oom_kill line in memory.events: {r.stdout!r}")


def read_cpu(container):
    """(usage_usec, nr_throttled, limit_cores or None, monotonic time) from the cgroup."""
    def cat(name):
        r = subprocess.run(["docker", "exec", container, "cat", f"/sys/fs/cgroup/{name}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"cannot read {name} from {container}: {r.stderr.strip()}")
        return r.stdout
    stat = {k: int(v) for k, v in (l.split() for l in cat("cpu.stat").splitlines())}
    quota, period = cat("cpu.max").split()  # e.g. "50000 100000", or "max 100000"
    limit = None if quota == "max" else int(quota) / int(period)
    return stat["usage_usec"], stat["nr_throttled"], limit, time.monotonic()


def read_data_disk(container):
    """Fraction (0-1) of the data volume in use, from df inside the container; None if unreadable."""
    r = subprocess.run(["docker", "exec", container, "df", "-B1", "--output=size,used", DATA_DIR],
                       capture_output=True, text=True)
    try:
        size, used = (int(x) for x in r.stdout.splitlines()[-1].split())
        return round(used / size, 3)
    except (ValueError, IndexError):
        return None


def probe_write(host, port, user, password, dbname):
    """A real INSERT (64 KB, uncompressed, so it must extend a file) then DELETE of that row.
    Returns (write_failed, error): True/error text if the INSERT raised, False/None if it
    worked, None/'could not connect' if the probe could not run at all."""
    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password,
                                dbname=dbname, connect_timeout=5)
    except psycopg2.Error as e:
        return None, f"probe could not connect: {str(e).strip()}"
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE}(id serial PRIMARY KEY, payload text)")
            cur.execute(f"ALTER TABLE {PROBE_TABLE} ALTER COLUMN payload SET STORAGE EXTERNAL")
            cur.execute(f"INSERT INTO {PROBE_TABLE}(payload) VALUES (repeat('x', {PROBE_BYTES})) "
                        "RETURNING id")
            row_id = cur.fetchone()[0]
            try:
                cur.execute(f"DELETE FROM {PROBE_TABLE} WHERE id = %s", (row_id,))
            except psycopg2.Error:
                pass  # cleanup of the probe row is best-effort; the INSERT already succeeded
        return False, None
    except psycopg2.Error as e:
        return True, str(e).strip()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def read_pg_ready_inside(container, user, dbname):
    """pg_isready run inside the container (local socket, no network). True/False, or None if
    it could not be run (e.g. the container is not running)."""
    r = subprocess.run(["docker", "exec", container, "pg_isready", "-U", user, "-d", dbname],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return True
    if r.returncode in (1, 2):   # pg_isready: 1 = rejecting connections, 2 = no response
        return False
    return None


def probe_connection(host, port, user, password, dbname):
    """(ok, error). A real connect + SELECT 1, not a port check."""
    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password,
                                dbname=dbname, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return True, None
    except psycopg2.Error as e:
        return False, str(e).strip()


def collect_signal(container, host, port, user, password, dbname, window):
    before = read_oom_kill(container)
    cpu0 = read_cpu(container)
    t0 = time.time()
    time.sleep(window)
    after = read_oom_kill(container)
    cpu1 = read_cpu(container)
    t_conn = time.monotonic()
    ok, err = probe_connection(host, port, user, password, dbname)
    connect_seconds = round(time.monotonic() - t_conn, 3)
    ready_inside = read_pg_ready_inside(container, user, dbname)
    write_failed, write_error = probe_write(host, port, user, password, dbname)
    limit = cpu1[2]
    cores = (cpu1[0] - cpu0[0]) / 1e6 / (cpu1[3] - cpu0[3])
    return {
        "data_disk_percent_used": read_data_disk(container),
        "write_failed": write_failed,
        "write_error": write_error,
        "cpu_limit_cores": limit,
        "cpu_avg_cores": round(cores, 3),
        "cpu_share_of_limit": round(cores / limit, 3) if limit else None,
        "cpu_throttled_periods": cpu1[1] - cpu0[1],
        "oom_kill_before": before,
        "oom_kill_after": after,
        "oom_kill_delta": after - before,
        "window_seconds": round(time.time() - t0, 1),
        "db_connect_ok": ok,
        "db_connect_error": err,
        "db_connect_seconds": connect_seconds,
        "pg_ready_inside": ready_inside,
    }


# ---------- model call + schema validation ----------

def validate(raw):
    """Parse and strictly validate model output. Returns dict or raises ValueError."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"not valid JSON: {e}")
    if not isinstance(obj, dict):
        raise ValueError("top-level value is not an object")
    if set(obj) != set(SCHEMA["required"]):
        raise ValueError(f"keys {sorted(obj)} != {sorted(SCHEMA['required'])}")
    for k in ("severity", "confidence"):
        v = obj[k]
        # bool is an int subclass in Python; reject it explicitly.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{k} must be a number, got {v!r}")
        if not math.isfinite(v) or not 0 <= v <= 1:
            raise ValueError(f"{k} out of range [0,1]: {v!r}")
    if not isinstance(obj["escalate"], bool):
        raise ValueError(f"escalate must be a boolean, got {obj['escalate']!r}")
    return {"severity": float(obj["severity"]), "escalate": obj["escalate"],
            "confidence": float(obj["confidence"])}


def ask_model(model, signal):
    keys = ("oom_kill_delta", "window_seconds", "db_connect_ok",
            "cpu_share_of_limit", "cpu_throttled_periods",
            "data_disk_percent_used", "write_failed", "write_error",
            "db_connect_seconds", "pg_ready_inside")
    facts = {k: signal[k] for k in keys if k in signal}
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=120, json={
        "model": model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Signal:\n" + json.dumps(facts)},
        ],
    })
    r.raise_for_status()
    return r.json()["message"]["content"]


def apply_floor(decision, signal):
    """Deterministic escalation floor. Returns (final_decision, overridden)."""
    floor = 0.0
    if signal["oom_kill_delta"] > 0:
        floor = max(floor, OOM_SEVERITY_FLOOR)
    share = signal.get("cpu_share_of_limit")  # absent/None -> CPU condition not met
    if (share is not None and share >= CPU_SHARE_THRESHOLD
            and signal.get("cpu_throttled_periods", 0) > 0):
        floor = max(floor, CPU_SEVERITY_FLOOR)
    disk = signal.get("data_disk_percent_used")  # absent/None -> that condition not met
    if signal.get("write_failed") is True or (disk is not None and disk >= DISK_USED_THRESHOLD):
        floor = max(floor, DISK_SEVERITY_FLOOR)
    if signal.get("db_connect_ok") is False and signal.get("pg_ready_inside") is True:
        floor = max(floor, NET_SEVERITY_FLOOR)
    if not floor:
        return decision, False
    final = dict(decision)
    final["escalate"] = True
    final["severity"] = max(decision["severity"], floor)
    return final, final != decision


def classify(signal, model=DEFAULT_MODEL, max_attempts=2):
    """Returns the final decision plus audit info. Never returns unvalidated model output.

    model_raw is the schema-validated model decision before the code floor (None if the
    model never produced valid output); overridden is True when the floor changed it.
    """
    attempts = []
    model_raw, source = None, "fallback"
    for _ in range(max_attempts):  # first try + one retry
        try:
            raw = ask_model(model, signal)
        except requests.RequestException as e:
            attempts.append({"raw": None, "error": f"ollama request failed: {e}"})
            continue
        try:
            model_raw = validate(raw)
        except ValueError as e:
            attempts.append({"raw": raw, "error": str(e)})
            continue
        attempts.append({"raw": raw, "error": None})
        source = "model"
        break
    if model_raw is None:
        # No schema-valid output: fail safe toward a human. Zero confidence marks it.
        decision = {"severity": 1.0, "escalate": True, "confidence": 0.0}
    else:
        decision = model_raw
    final, overridden = apply_floor(decision, signal)
    return {"decision": final, "model_raw": model_raw, "overridden": overridden,
            "source": source, "model": model, "attempts": attempts, "signal": signal}


def main():
    cfg = load_env()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--container", default="opsmind-postgres")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=int(cfg.get("POSTGRES_HOST_PORT", 5433)))
    p.add_argument("--user", default=cfg.get("POSTGRES_USER"))
    p.add_argument("--password", default=cfg.get("POSTGRES_PASSWORD"))
    p.add_argument("--dbname", default=cfg.get("POSTGRES_DB"))
    p.add_argument("--window", type=int, default=30,
                   help="seconds between the two cgroup reads")
    p.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args()
    if not (args.user and args.password and args.dbname):
        sys.exit(f"missing POSTGRES_* settings: set them in {ENV_FILE} or the environment")

    print(f"sampling cgroup memory/cpu for {args.window}s (trigger the fault now)...",
          file=sys.stderr)
    signal = collect_signal(args.container, args.host, args.port, args.user,
                            args.password, args.dbname, args.window)
    print(json.dumps(classify(signal, args.model), indent=2))


if __name__ == "__main__":
    main()

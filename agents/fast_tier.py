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
container, i.e. a network partition) OR the dependent app's /health returned a non-200 HTTP
status OR the monitored container is not running. Cgroup facts are None (not 0) when the
container is not running, because they could not be measured.

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
import urllib.error
import urllib.request
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
SERVICE_SEVERITY_FLOOR = 0.8   # app /health non-200, or the monitored container not running
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
- app_health_status is the HTTP status of the dependent web app's /health (200 = healthy;
  503 = the app says it is unhealthy; null = could not be measured). app_container_state and
  container_state are Docker's state for the app and for the database container
  ("running", "exited", ...). container_state other than "running" means the database
  container itself is down. When container_state is not "running", the cgroup, disk and
  connection facts are null because they could not be measured.
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


def container_state(name):
    """Docker's State.Status for a container ("running", "exited", ...), or None if it does not
    exist / docker could not be asked."""
    r = subprocess.run(["docker", "inspect", "--format", "{{.State.Status}}", name],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def probe_app_health(port, timeout=5):
    """Real HTTP GET of the app's /health. Returns (http_status or None, error or None):
    the status is None only when the app could not be reached at all."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=timeout) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error")
        except (ValueError, AttributeError):
            err = None
        return e.code, err
    except (urllib.error.URLError, OSError) as e:
        return None, f"app unreachable: {e}"


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


def collect_signal(container, host, port, user, password, dbname, window, app=None,
                   app_port=None):
    """Sample the monitored container (and, if `app`/`app_port` are given, the web app that
    depends on it). Unmeasurable facts are None, never a made-up 0."""
    state = container_state(container)
    extra = {"container_state": state}
    if app and app_port:
        app_status, app_err = probe_app_health(app_port)
        extra.update({"app_container_state": container_state(app),
                      "app_health_status": app_status, "app_health_error": app_err})
    if state != "running":
        ok, err = probe_connection(host, port, user, password, dbname)
        return {**extra, "data_disk_percent_used": None, "write_failed": None,
                "write_error": None, "cpu_limit_cores": None, "cpu_avg_cores": None,
                "cpu_share_of_limit": None, "cpu_throttled_periods": None,
                "oom_kill_before": None, "oom_kill_after": None, "oom_kill_delta": None,
                "window_seconds": 0.0, "db_connect_ok": ok, "db_connect_error": err,
                "db_connect_seconds": None, "pg_ready_inside": None}
    sig = _collect_running(container, host, port, user, password, dbname, window)
    return {**extra, **sig}


def _collect_running(container, host, port, user, password, dbname, window):
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

USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "model_seconds": 0.0}


def record_usage(resp_json):
    """Accumulate Ollama's own counters for a response (side channel; callers' return values
    are unchanged). Read-and-reset with take_usage()."""
    USAGE["calls"] += 1
    USAGE["prompt_tokens"] += resp_json.get("prompt_eval_count") or 0
    USAGE["completion_tokens"] += resp_json.get("eval_count") or 0
    USAGE["model_seconds"] += (resp_json.get("total_duration") or 0) / 1e9


def take_usage():
    """Return the accumulated usage and reset it."""
    out = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in USAGE.items()}
    USAGE.update(calls=0, prompt_tokens=0, completion_tokens=0, model_seconds=0.0)
    return out


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
            "db_connect_seconds", "pg_ready_inside", "container_state",
            "app_health_status", "app_container_state", "app_health_error")
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
    body = r.json()
    record_usage(body)
    return body["message"]["content"]


def apply_floor(decision, signal):
    """Deterministic escalation floor. Returns (final_decision, overridden)."""
    floor = 0.0
    if (signal.get("oom_kill_delta") or 0) > 0:   # absent/None -> not measured -> not met
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
    status = signal.get("app_health_status")
    if isinstance(status, int) and not isinstance(status, bool) and status != 200:
        floor = max(floor, SERVICE_SEVERITY_FLOOR)
    cstate = signal.get("container_state")
    if cstate is not None and cstate != "running":
        floor = max(floor, SERVICE_SEVERITY_FLOOR)
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
    p.add_argument("--app", default="opsmind-app",
                   help="container name of the web app that depends on the database")
    p.add_argument("--app-port", type=int, default=int(cfg.get("APP_HOST_PORT", 5050)))
    p.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args()
    if not (args.user and args.password and args.dbname):
        sys.exit(f"missing POSTGRES_* settings: set them in {ENV_FILE} or the environment")

    print(f"sampling cgroup memory/cpu for {args.window}s (trigger the fault now)...",
          file=sys.stderr)
    signal = collect_signal(args.container, args.host, args.port, args.user,
                            args.password, args.dbname, args.window,
                            app=args.app, app_port=args.app_port)
    print(json.dumps(classify(signal, args.model), indent=2))


if __name__ == "__main__":
    main()

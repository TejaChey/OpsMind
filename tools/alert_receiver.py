"""Alert receiver: lets OpsMind sit BEHIND standard monitoring (Prometheus + Alertmanager).

Companies already have alerting; a remediation engine has to plug in behind it. This is an
Alertmanager webhook receiver (payload format version 4). Design rules:

  * An alert is a TRIGGER, never a fact. Alert text can be stale, wrong or attacker-controlled,
    so nothing from it is shown to a model. Each triage re-measures everything itself
    (agents/supervisor.py collects a fresh signal); the alert is only recorded as provenance.
  * Authenticated: Bearer token from services/monitoring/webhook_token, constant-time compare.
    Without a token file the receiver refuses to start (fail closed).
  * One incident at a time: a single worker runs triages in order, so two fixes never race.
  * Bounded: at most QUEUE_MAX triages waiting; when full it answers 503 and Alertmanager
    retries later (back-pressure instead of an unbounded pile-up).
  * Deduplicated: one triage per firing alert (fingerprint + startsAt). Alertmanager re-sends a
    still-firing alert every repeat_interval; that does NOT start another triage (repeats are the
    governor's business, not the pager's). A NEW firing of the same alert does.
  * Allow-listed: only alert names in TRIAGE_ALERTS start a triage; others are logged and ignored.
  * Every event (received, queued, ignored, duplicate, triaged, resolved) is appended to
    trajectories/alerts.jsonl, linked to the triage run_id, so alert -> run -> fix -> resolved
    can be traced and timed in code.

Usage:
  python tools/alert_receiver.py --init-token      # create the token file once
  python tools/alert_receiver.py [--live] [--port 9095] [--window 5]
"""
import argparse
import hmac
import json
import queue
import re
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.jsonl_io import append_jsonl  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / "services" / "monitoring" / "webhook_token"
LEDGER_FILE = ROOT / "trajectories" / "alerts.jsonl"
RUNS_FILE = ROOT / "trajectories" / "runs.jsonl"
TRIAGE_ALERTS = {"OpsMindAppUnhealthy", "OpsMindAppDown"}
QUEUE_MAX = 10
MAX_BODY = 1_000_000
TRIAGE_TIMEOUT = 600


class BadPayload(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_payload(body):
    """Alertmanager webhook v4 -> list of alerts. Raises BadPayload on anything malformed:
    a receiver that guesses at bad input is a way in for bad input."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise BadPayload(f"not JSON: {e}")
    if not isinstance(data, dict) or data.get("version") != "4":
        raise BadPayload("expected an Alertmanager webhook payload with version '4'")
    alerts = data.get("alerts")
    if not isinstance(alerts, list):
        raise BadPayload("'alerts' must be a list")
    out = []
    for a in alerts:
        if not isinstance(a, dict) or a.get("status") not in ("firing", "resolved") \
                or not isinstance(a.get("labels"), dict) \
                or not isinstance(a.get("fingerprint"), str) \
                or not isinstance(a.get("startsAt"), str):
            raise BadPayload("each alert needs status firing|resolved, labels, fingerprint, startsAt")
        out.append({"status": a["status"], "alertname": str(a["labels"].get("alertname", "")),
                    "labels": {str(k): str(v) for k, v in a["labels"].items()},
                    "fingerprint": a["fingerprint"], "startsAt": a["startsAt"],
                    "endsAt": a.get("endsAt")})
    return out


def run_status(run_id, runs_file=RUNS_FILE):
    """What the triage run decided, read back from the trajectory log (not from its stdout)."""
    if not run_id or not Path(runs_file).exists():
        return {}
    for line in reversed(Path(runs_file).read_text(encoding="utf-8").splitlines()):
        if run_id in line:
            r = json.loads(line)
            return {"escalated": (r.get("decision") or {}).get("escalate"),
                    "mapped_action": r.get("mapped_action"),
                    "execution_status": (r.get("execution") or {}).get("status"),
                    "verified": (r.get("verification") or {}).get("verified")}
    return {}


def supervisor_runner(live, window):
    """Runs one triage as a separate process (a crash there cannot take the receiver down)."""
    def run(trigger):
        cmd = [sys.executable, str(ROOT / "agents" / "supervisor.py"), "--window", str(window),
               "--trigger", json.dumps(trigger)] + (["--live"] if live else [])
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TRIAGE_TIMEOUT, cwd=ROOT)
        m = re.search(r"logged run (\S+)", p.stderr)
        return {"exit_code": p.returncode, "run_id": m.group(1) if m else None,
                "stderr_tail": p.stderr.splitlines()[-5:]}
    return run


class Receiver:
    """Transport-free core (tested without HTTP): dedup, allow-list, queue, ledger."""

    def __init__(self, runner, ledger=LEDGER_FILE, runs_file=RUNS_FILE, queue_max=QUEUE_MAX):
        self.runner, self.ledger, self.runs_file = runner, ledger, runs_file
        self.q = queue.Queue(maxsize=queue_max)
        self.lock = threading.Lock()
        self.seen = {}          # (fingerprint, startsAt) -> run_id (None while queued)

    def log(self, event, alert=None, **extra):
        entry = {"at": now(), "event": event, **extra}
        if alert:
            entry.update({k: alert[k] for k in ("alertname", "fingerprint", "startsAt")})
        append_jsonl(self.ledger, entry)
        return entry

    def handle(self, alerts):
        """Returns (http_status, summary). 503 only if a NEW firing alert did not fit in the
        queue (Alertmanager will retry; nothing is marked seen, so the retry is not a dup)."""
        summary = {"queued": 0, "duplicate": 0, "ignored": 0, "resolved": 0}
        status = 200
        for a in alerts:
            key = (a["fingerprint"], a["startsAt"])
            if a["status"] == "resolved":
                with self.lock:
                    run_id = self.seen.get(key)
                self.log("resolved", a, endsAt=a["endsAt"], run_id=run_id)
                summary["resolved"] += 1
                continue
            if a["alertname"] not in TRIAGE_ALERTS:
                self.log("ignored", a, why="alert name not in TRIAGE_ALERTS")
                summary["ignored"] += 1
                continue
            with self.lock:
                if key in self.seen:
                    summary["duplicate"] += 1
                    continue                    # repeat notification of the same firing
                try:
                    self.q.put_nowait(a)
                except queue.Full:
                    status = 503
                    self.log("rejected_queue_full", a)
                    continue
                self.seen[key] = None
            self.log("queued", a, queue_depth=self.q.qsize())
            summary["queued"] += 1
        return status, summary

    def work_one(self):
        a = self.q.get()
        try:
            trigger = {"source": "alertmanager", **{k: a[k] for k in
                       ("alertname", "fingerprint", "startsAt", "labels")}}
            try:
                result = self.runner(trigger)
            except Exception as e:      # a failed triage is logged, never kills the worker
                result = {"exit_code": None, "run_id": None, "error": f"{type(e).__name__}: {e}"}
            with self.lock:
                self.seen[(a["fingerprint"], a["startsAt"])] = result.get("run_id")
            self.log("triaged", a, **result, **run_status(result.get("run_id"), self.runs_file))
        finally:
            self.q.task_done()

    def worker(self):
        while True:
            self.work_one()


def make_handler(receiver, token):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                return self.reply(200, {"ok": True, "queue_depth": receiver.q.qsize()})
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/alerts":
                return self.reply(404, {"error": "not found"})
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
                return self.reply(401, {"error": "bad or missing bearer token"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                return self.reply(413 if n > MAX_BODY else 400, {"error": "bad body size"})
            try:
                alerts = parse_payload(self.rfile.read(n))
            except BadPayload as e:
                return self.reply(400, {"error": str(e)})
            code, summary = receiver.handle(alerts)
            self.reply(code, summary)

        def log_message(self, fmt, *args):   # quiet; the ledger is the record
            pass
    return Handler


def report(ledger=LEDGER_FILE, runs_file=RUNS_FILE):
    """One row per firing alert that was triaged: how long the ALERT was open (Alertmanager's
    own startsAt -> endsAt, i.e. what the monitoring system saw) and what OpsMind did. Computed
    in code from the ledger; still-open alerts have minutes_open None."""
    rows = {}
    for line in Path(ledger).read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        key = (e.get("fingerprint"), e.get("startsAt"))
        if e["event"] == "triaged":
            rows[key] = {"alertname": e["alertname"], "startsAt": e["startsAt"],
                         "run_id": e.get("run_id"), "mapped_action": e.get("mapped_action"),
                         "execution_status": e.get("execution_status"),
                         "verified": e.get("verified"), "minutes_open": None}
        elif e["event"] == "resolved" and key in rows:
            start = datetime.fromisoformat(e["startsAt"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(e["endsAt"].replace("Z", "+00:00"))
            rows[key]["minutes_open"] = round((end - start).total_seconds() / 60, 2)
    return list(rows.values())


def init_token(path=TOKEN_FILE):
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    path.chmod(0o600)
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--init-token", action="store_true", help="create the token file and exit")
    p.add_argument("--report", action="store_true", help="print alert -> fix -> resolved rows and exit")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; Docker Desktop's host.docker.internal reaches loopback "
                        "(verified on macOS). On Linux Docker you may need the bridge address")
    p.add_argument("--port", type=int, default=9095)
    p.add_argument("--window", type=int, default=5, help="supervisor sampling window (seconds)")
    p.add_argument("--live", action="store_true",
                   help="triages may execute policy-approved actions (default: dry run)")
    a = p.parse_args()
    if a.init_token:
        print("created" if init_token() else "exists", TOKEN_FILE)
        return
    if a.report:
        for row in report():
            print(json.dumps(row))
        return
    if not TOKEN_FILE.exists():
        sys.exit(f"no token file at {TOKEN_FILE}; run with --init-token first (refusing to run "
                 "an unauthenticated receiver)")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    receiver = Receiver(supervisor_runner(a.live, a.window))
    threading.Thread(target=receiver.worker, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(receiver, token))
    print(f"alert receiver on {a.host}:{a.port} ({'LIVE' if a.live else 'dry run'}); "
          f"ledger {LEDGER_FILE}", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

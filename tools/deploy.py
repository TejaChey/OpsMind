"""Deploy history for the web app's config: the "deploys/" record the brief asks for.

A deploy = write a config file the app reads (deploys/live/app.json, mounted into the app
container) + append one line to deploys/history.jsonl: id, timestamp, the full config, and a
status that CODE decides by really calling the app's /health after the deploy ("good"/"bad").
The "last known-good" deploy is simply the newest entry whose status is "good".

Used by faults/config_regression.py (to deploy a bad config), by the supervisor (recent deploy
facts for the slow tier), by the executor (ROLLBACK_CONFIG) and by verification.
Run `python tools/deploy.py show` to print the history.
"""
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOYS_DIR = ROOT / "deploys"
HISTORY_FILE = DEPLOYS_DIR / "history.jsonl"
LIVE_FILE = DEPLOYS_DIR / "live" / "app.json"
GOOD_DEFAULT = {"db_host": "postgres", "db_port": 5432, "connect_timeout_seconds": 2}


def app_is_healthy(app_port, timeout=5):
    """Real GET of the app's /health. Returns (ok, detail)."""
    try:
        with urllib.request.urlopen(f"http://localhost:{app_port}/health", timeout=timeout) as r:
            body = json.loads(r.read().decode())
            return r.status == 200 and body.get("db_ok") is True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


STABLE_PROBES = 3   # consecutive healthy probes (1 s apart) before a deploy counts as good


def wait_healthy(app_port, seconds=15, stable=STABLE_PROBES):
    """Poll /health once a second until it has been healthy `stable` times IN A ROW, or
    `seconds` pass. One early healthy reading is not enough: right after a deploy the app may
    still be running on the old config (file propagation lag), which would be a false 'good'.
    Returns (ok, detail)."""
    deadline, streak, last = time.monotonic() + seconds, 0, (False, "not polled")
    while True:
        last = app_is_healthy(app_port)
        streak = streak + 1 if last[0] else 0
        if streak >= stable:
            return True, f"{last[1]} x{streak} in a row"
        if time.monotonic() >= deadline:
            return False, last[1] if not last[0] else f"only {streak} healthy in a row"
        time.sleep(1)


def read_history(path=HISTORY_FILE):
    if not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def append_history(entry, path=HISTORY_FILE):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_live(path=LIVE_FILE):
    """The config currently on disk for the app, or None if there is none."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_live(config, path=LIVE_FILE):
    """Atomic replace, so the app never reads a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, sort_keys=True)
    os.chmod(tmp, 0o644)  # the app container must be able to read it
    os.replace(tmp, path)


def last_good(history):
    """Newest history entry whose code-decided status is 'good', or None."""
    for entry in reversed(history):
        if entry.get("status") == "good":
            return entry
    return None


def diff(old, new):
    """{key: {'from': old value, 'to': new value}} for every key that differs."""
    old, new = old or {}, new or {}
    return {k: {"from": old.get(k), "to": new.get(k)}
            for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)}


def _now():
    return datetime.now(timezone.utc)


def deploy(config, label, app_port, kind="deploy", restores=None, settle=15,
           history_path=HISTORY_FILE, live_path=LIVE_FILE):
    """Write `config` as the live config, check the app for real, and record the entry."""
    history = read_history(history_path)
    write_live(config, live_path)
    ok, detail = wait_healthy(app_port, settle)
    entry = {"deploy_id": uuid.uuid4().hex[:8], "timestamp": _now().isoformat(), "kind": kind,
             "label": label, "config": config,
             "previous_id": history[-1]["deploy_id"] if history else None,
             "status": "good" if ok else "bad", "health_after": detail}
    if restores:
        entry["restores"] = restores
    append_history(entry, history_path)
    return entry


def ensure_baseline(app_port, history_path=HISTORY_FILE, live_path=LIVE_FILE):
    """Make sure there is at least one good deploy to roll back to (deploy the default)."""
    if last_good(read_history(history_path)) is None:
        return deploy(dict(GOOD_DEFAULT), "baseline", app_port, history_path=history_path,
                      live_path=live_path)
    return None


def rollback_target(history_path=HISTORY_FILE, live_path=LIVE_FILE):
    """What a rollback would do, decided from files: (target entry or None, reason).
    Nothing to do if there is no good deploy, or the live config already equals it."""
    good = last_good(read_history(history_path))
    if good is None:
        return None, "no known-good deploy in history"
    if read_live(live_path) == good["config"]:
        return None, f"live config already equals known-good deploy {good['deploy_id']}"
    return good, "live config differs from the last known-good deploy"


def recent_deploy_facts(history_path=HISTORY_FILE, live_path=LIVE_FILE, now=None):
    """Facts for the tiers about the newest deploy, or None if there is no history:
    id, age, code-decided status, and which keys changed vs the last known-good deploy
    (the 'deploy diff'). Pure facts: no verdict about whether the deploy caused anything."""
    history = read_history(history_path)
    if not history:
        return None
    newest = history[-1]
    good = last_good(history)
    age = ((now or _now()) - datetime.fromisoformat(newest["timestamp"])).total_seconds()
    base = good["config"] if good and good["deploy_id"] != newest["deploy_id"] else None
    facts = {"deploy_id": newest["deploy_id"], "kind": newest["kind"],
             "age_seconds": round(age, 1), "status_when_deployed": newest["status"]}
    if base is not None:
        facts["changed_vs_last_known_good"] = diff(base, newest["config"])
    return facts


if __name__ == "__main__":
    for e in read_history():
        print(e["deploy_id"], e["timestamp"], e["kind"], e["status"], e["label"], e["config"])
    print("live:", read_live())

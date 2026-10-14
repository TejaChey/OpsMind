"""Fault: dependency down. Really stops the Postgres container (`docker stop`) that the web app
depends on. The app process keeps running, but its /health (a real database round trip) now
returns 503: the symptom shows up in the app, the cause is in a different container.

Usage:  python faults/dependency_down.py [--app NAME] [--dependency NAME] [--app-port P]
                                         [--wait S] [--hold | --clear]

--hold leaves Postgres stopped for an agent to observe; --clear starts it again (the matching
manual cleanup). Without either, the script restarts Postgres at the end.

Prints FAULT TOOK EFFECT only if ALL are measured true: the app's /health was 200 before and is
503 after; the app container is still running; the dependency container is NOT running.
No agent logic.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / "services" / ".env"


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


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def state(container):
    return json.loads(docker("inspect", container).stdout)[0]["State"]["Status"]


def app_health(port, timeout=5):
    """Real HTTP GET of the app's /health. Returns (http_status or None, body or error)."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def wait_for_app(port, want_ok, seconds):
    """Poll /health until it is (or is no longer) 200. Returns the last (status, body)."""
    deadline, last = time.time() + seconds, (None, "not polled")
    while time.time() < deadline:
        last = app_health(port)
        if (last[0] == 200) == want_ok:
            return last
        time.sleep(1)
    return last


def start_dependency(args):
    docker("start", args.dependency)
    print(f"started {args.dependency}; waiting up to {args.wait}s for app /health = 200")
    st, body = wait_for_app(args.app_port, True, args.wait)
    print(f"app /health after start: {st} {body.strip()}")


def main():
    cfg = load_env()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--app", default="opsmind-app")
    p.add_argument("--dependency", default="opsmind-postgres")
    p.add_argument("--app-port", type=int, default=int(cfg.get("APP_HOST_PORT", 5050)))
    p.add_argument("--wait", type=int, default=60, help="seconds to wait for health to change")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--hold", action="store_true", help="leave the dependency stopped")
    g.add_argument("--clear", action="store_true", help="only start the dependency, then exit")
    args = p.parse_args()
    if args.clear:
        return start_dependency(args)

    if state(args.dependency) != "running":
        sys.exit(f"{args.dependency} is not running; run with --clear first")
    st_b, body_b = app_health(args.app_port)
    print(f"before: app={state(args.app)} dependency={state(args.dependency)} "
          f"app /health -> {st_b} {body_b.strip()}")
    if st_b != 200:
        sys.exit("app is not healthy even before the fault; fix that first")

    docker("stop", args.dependency)
    try:
        st_a, body_a = wait_for_app(args.app_port, False, args.wait)
        app_a, dep_a = state(args.app), state(args.dependency)
        print(f"after:  app={app_a} dependency={dep_a} app /health -> {st_a} {body_a.strip()}")
    finally:
        if args.hold:
            print("hold: dependency left stopped; run with --clear to start it")
        else:
            start_dependency(args)

    if st_b == 200 and st_a == 503 and app_a == "running" and dep_a != "running":
        print("FAULT TOOK EFFECT (app /health 200 -> 503; app container running; "
              f"dependency is {dep_a})")
    else:
        why = []
        if st_a != 503:
            why.append(f"app /health after stop was {st_a}, not 503")
        if app_a != "running":
            why.append(f"app container is {app_a}")
        if dep_a == "running":
            why.append("dependency still running")
        print("NO DEPENDENCY-DOWN FAULT OBSERVED: " + "; ".join(why))


if __name__ == "__main__":
    main()

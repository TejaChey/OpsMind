"""A small, REAL CI pipeline used as OpsMind's second domain (Phase 2).

It runs five steps in a git workspace, captures their real output in a GitHub-Actions-style
log, and appends every run to a history file. Nothing here is simulated: a step fails because
a real command exited non-zero (a refused connection, a missing module, a failing test).

Steps: checkout (read HEAD) -> fetch (HTTP GET an artifact) -> deps (import every module in
requirements.txt) -> compile (byte-compile src/) -> test (python -m unittest).

Why a local pipeline and not GitHub Actions logs: GitHub only serves job logs to an
authenticated caller (an unauthenticated request returns 403), so a reproducible, token-free
loop needs a pipeline we run ourselves. Everything runs from the repo root.
"""
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.jsonl_io import append_jsonl

CI_DIR = Path(__file__).resolve().parent
STATE = CI_DIR / "state"                      # runtime state, ignored by git (see state/.gitignore)
WORKSPACE = STATE / "workspace"               # the project under test (its own git repo)
RUNNER_SITE = STATE / "runner_site"           # the "installed packages" of the CI runner
HISTORY = STATE / "history.jsonl"
FETCH_PORT = 5077
FETCH_URL = f"http://localhost:{FETCH_PORT}/artifact.json"
STEP_TIMEOUT = 30

FETCH_CODE = ("import urllib.request, sys; "
              f"d = urllib.request.urlopen('{FETCH_URL}', timeout=3).read(); "
              "open('artifact.json', 'wb').write(d); print('fetched', len(d), 'bytes')")
DEPS_CODE = ("import importlib, sys; "
             "mods = [l.strip() for l in open('requirements.txt') if l.strip()]; "
             "[importlib.import_module(m) for m in mods]; print('imported', ', '.join(mods))")
STEPS = [
    ("fetch", [sys.executable, "-c", FETCH_CODE]),
    ("deps", [sys.executable, "-c", DEPS_CODE]),
    ("compile", [sys.executable, "-m", "compileall", "-q", "src"]),
    ("test", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
]


def git(*args, cwd=WORKSPACE):
    r = subprocess.run(["git", "-c", "user.name=ci", "-c", "user.email=ci@example.invalid",
                        *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def head_sha():
    return git("rev-parse", "HEAD")


def init_workspace(force=False):
    """Create the workspace (a fresh git repo with one green commit) and the runner packages.
    With force=True an existing one is deleted first. Returns the HEAD sha."""
    if force and STATE.exists():
        for child in STATE.iterdir():
            if child.name != ".gitignore":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    if not WORKSPACE.exists():
        shutil.copytree(CI_DIR / "sample_project", WORKSPACE)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "initial commit")
    if not RUNNER_SITE.exists():
        shutil.copytree(CI_DIR / "runner_site", RUNNER_SITE)
    return head_sha()


def read_history():
    if not HISTORY.exists():
        return []
    return [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_pipeline(record=True):
    """Run all steps on the current workspace. Stops at the first failing step (like CI).
    Returns {run_id, sha, conclusion, failed_step, exit_code, steps, log}."""
    import os
    env = dict(os.environ, PYTHONPATH=f"{RUNNER_SITE}:{WORKSPACE / 'src'}", PYTHONDONTWRITEBYTECODE="1")
    run_id, sha = uuid.uuid4().hex[:8], head_sha()
    log, steps, failed, code = [f"##[group]Checkout\nHEAD is {sha}\n##[endgroup]"], [], None, 0
    for name, cmd in STEPS:
        t = time.monotonic()
        try:
            r = subprocess.run(cmd, cwd=WORKSPACE, env=env, capture_output=True, text=True,
                               timeout=STEP_TIMEOUT)
            out, rc = (r.stdout + r.stderr).strip(), r.returncode
        except subprocess.TimeoutExpired:
            out, rc = f"step exceeded {STEP_TIMEOUT}s and was killed", 124
        seconds = round(time.monotonic() - t, 2)
        log.append(f"##[group]Run {name}\n{out}\n##[endgroup]")
        steps.append({"name": name, "exit_code": rc, "seconds": seconds})
        if rc != 0:
            log.append(f"##[error]Process completed with exit code {rc}.")
            failed, code = name, rc
            break
    result = {"run_id": run_id, "sha": sha, "conclusion": "failure" if failed else "success",
              "failed_step": failed, "exit_code": code, "steps": steps, "log": "\n".join(log)}
    if record:
        append_jsonl(HISTORY, {"run_id": run_id, "ts": datetime.now(timezone.utc).isoformat(),
                               "sha": sha, "conclusion": result["conclusion"],
                               "failed_step": failed})
    return result


if __name__ == "__main__":
    init_workspace()
    res = run_pipeline()
    print(res["log"])
    print(f"\nconclusion: {res['conclusion']} (run {res['run_id']}, sha {res['sha'][:7]})")

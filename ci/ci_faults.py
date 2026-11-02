"""Fault injection for the CI domain (Phase 2). Each fault really breaks the pipeline; none is
simulated. Usage:  python ci/ci_faults.py {flaky-network|test-regression|missing-dependency|reset}

  flaky-network      the artifact server answers 503 once, then recovers by itself (an outage
                     that a plain re-run of the SAME commit survives)
  test-regression    a new commit changes mean() so a real test fails (the fix is to revert it)
  missing-dependency the runner loses a package listed in requirements.txt while the commit is
                     unchanged and was green before (a re-run CANNOT help: a trap for retry-on-red)
  reset              restore the green baseline (workspace commit, runner packages, server flag)
"""
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ci import pipeline

FAULTS = ("flaky-network", "test-regression", "missing-dependency")
FLAG = pipeline.STATE / "flaky_once"
SERVER_PID = pipeline.STATE / "server.pid"


def server_up():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", pipeline.FETCH_PORT)) == 0


def ensure_server():
    """Start the artifact server (a separate background process) if it is not answering."""
    if server_up():
        return
    p = subprocess.Popen([sys.executable, str(Path(__file__).parent / "flaky_server.py"),
                          "--port", str(pipeline.FETCH_PORT), "--flag", str(FLAG)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    SERVER_PID.write_text(str(p.pid))
    for _ in range(40):
        if server_up():
            return
        time.sleep(0.1)
    raise RuntimeError("artifact server did not start")


def baseline_sha():
    """The first (green) commit of the workspace: the known-good state every reset returns to."""
    return pipeline.git("rev-list", "--max-parents=0", "HEAD")


def reset():
    pipeline.init_workspace()
    ensure_server()
    pipeline.git("reset", "-q", "--hard", baseline_sha())
    pipeline.git("clean", "-q", "-fd")
    FLAG.unlink(missing_ok=True)
    if pipeline.RUNNER_SITE.exists():
        shutil.rmtree(pipeline.RUNNER_SITE)
    shutil.copytree(pipeline.CI_DIR / "runner_site", pipeline.RUNNER_SITE)
    return pipeline.head_sha()


def inject(fault):
    """Break the (green) pipeline. Returns a one-line description of what was done."""
    if fault == "flaky-network":
        ensure_server()
        FLAG.write_text("1")
        return "artifact server will answer 503 to the next request, then recover"
    if fault == "test-regression":
        f = pipeline.WORKSPACE / "src" / "calc.py"
        f.write_text(f.read_text().replace("sum(values) / len(values)",
                                           "sum(values) / (len(values) + 1)"))
        pipeline.git("commit", "-q", "-am", "perf: simplify mean()")
        return "committed a change to mean() that breaks test_mean"
    if fault == "missing-dependency":
        shutil.rmtree(pipeline.RUNNER_SITE / "ci_fixture_lib")
        return "removed ci_fixture_lib from the runner's packages (commit unchanged)"
    raise ValueError(f"unknown fault {fault!r}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "reset":
        print("reset to", reset()[:10])
    elif cmd in FAULTS:
        reset()
        print(inject(cmd))
    else:
        sys.exit(__doc__)

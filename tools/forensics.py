"""Evidence snapshot taken right BEFORE a live action changes anything.

A restart or rollback can destroy the evidence of why something broke (a restarted container
starts a fresh log tail; a crash-looping one may be recreated). Practitioners' complaint about
auto-remediation is that it "fixes" the incident and the team learns nothing. So, just before a
live action, the supervisor saves each affected container's state and its last log lines into
the trajectory. Best effort: a failure here is recorded and never blocks the fix.
"""
import subprocess

LOG_LINES = 50


def snapshot(containers, log_lines=LOG_LINES):
    out = {}
    for name in sorted(set(containers)):
        item = {}
        try:
            item["state"] = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} "
                 "started={{.State.StartedAt}} restarts={{.RestartCount}}", name],
                capture_output=True, text=True, timeout=10).stdout.strip()
            logs = subprocess.run(["docker", "logs", "--tail", str(log_lines), name],
                                  capture_output=True, text=True, timeout=10)
            item["log_tail"] = (logs.stdout + logs.stderr).splitlines()[-log_lines:]
        except Exception as e:   # evidence only: never block the action
            item["error"] = f"{type(e).__name__}: {e}"
        out[name] = item
    return out

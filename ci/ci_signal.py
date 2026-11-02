"""Turn one pipeline run into MEASURED facts (the CI equivalent of fast_tier.collect_signal).

Everything here is plain code: regexes over the real log and `git` queries. No model decides
what the error class is. A fact that cannot be measured is None, never a guess.
"""
import re
import subprocess

from ci import pipeline

# Ordered: the first class whose pattern appears in the failed step's log wins.
ERROR_CLASSES = [
    ("module_not_found", r"ModuleNotFoundError|ImportError: No module"),
    ("syntax_error", r"SyntaxError|IndentationError"),
    ("timeout", r"exceeded \d+s and was killed"),
    ("network_error", r"HTTP Error 5\d\d|Connection refused|URLError|Temporary failure|"
                      r"Connection reset|timed out"),
    ("test_failure", r"^(FAIL|ERROR): |AssertionError"),
]
TRANSIENT = {"network_error", "timeout"}   # classes where an unchanged re-run can plausibly help
LOG_EXCERPT_LINES = 14


def failed_step_log(log, step):
    """The text of the `##[group]Run <step>` block (without the markers)."""
    m = re.search(rf"##\[group\]Run {re.escape(step)}\n(.*?)\n##\[endgroup\]", log, re.S)
    return m.group(1) if m else ""


def classify_error(text):
    """(error_class, first matching line) for a failed step's log."""
    for name, pat in ERROR_CLASSES:
        for line in text.splitlines():
            if re.search(pat, line):
                return name, line.strip()
    return "unknown", (text.strip().splitlines() or [""])[-1]


def _git_ok(*args):
    try:
        return pipeline.git(*args)
    except RuntimeError:
        return None


def collect_signal(result, history=None):
    """`result` is pipeline.run_pipeline()'s return value (the run just recorded in history)."""
    history = pipeline.read_history() if history is None else history
    sha = result["sha"]
    before = [h for h in history if h["run_id"] != result["run_id"]]   # earlier runs only
    greens = [h for h in before if h["conclusion"] == "success"]
    last_green = greens[-1]["sha"] if greens else None
    sig = {"domain": "ci", "conclusion": result["conclusion"], "head_sha": sha[:10],
           "failed_step": result["failed_step"], "exit_code": result["exit_code"],
           "step_seconds": {s["name"]: s["seconds"] for s in result["steps"]},
           "last_green_sha": last_green[:10] if last_green else None,
           "head_is_last_green": last_green == sha if last_green else None,
           "same_sha_passed_before": any(h["sha"] == sha for h in greens)}
    run = 0
    for h in reversed(before):   # consecutive failures on this exact commit, before this run
        if h["sha"] == sha and h["conclusion"] == "failure":
            run += 1
        else:
            break
    sig["earlier_failures_on_head"] = run
    if last_green and last_green != sha:
        count = _git_ok("rev-list", "--count", f"{last_green}..{sha}")
        files = _git_ok("diff", "--name-only", last_green, sha)
        sig["commits_since_green"] = int(count) if count is not None else None
        sig["changed_files_since_green"] = files.splitlines() if files is not None else None
    else:
        sig["commits_since_green"] = 0 if last_green else None
        sig["changed_files_since_green"] = [] if last_green else None
    if result["conclusion"] == "failure":
        text = failed_step_log(result["log"], result["failed_step"])
        sig["error_class"], sig["error_line"] = classify_error(text)
        sig["log_excerpt"] = "\n".join(text.splitlines()[-LOG_EXCERPT_LINES:])
    else:
        sig["error_class"], sig["error_line"], sig["log_excerpt"] = None, None, None
    return sig

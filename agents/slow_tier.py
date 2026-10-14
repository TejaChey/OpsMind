"""Slow reasoning tier: diagnose an escalated incident -> {root_cause, proposed_fix, confidence}.

Only called by the Supervisor when the fast tier escalates. Input is the fast tier's result
(signal, model_raw, final decision, overridden). Uses a larger local model via Ollama. Output
is schema-validated in code; on malformed output we retry once, then fall back to a generic
"restart the service" proposal. This tier only PROPOSES: nothing is executed here.
"""
import json
import math
import os

import requests

from fast_tier import OLLAMA_URL

DEFAULT_MODEL = os.environ.get("OPSMIND_SLOW_MODEL", "qwen2.5:7b")

SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "minLength": 1},
        "proposed_fix": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["root_cause", "proposed_fix", "confidence"],
    "additionalProperties": False,
}

FALLBACK = {
    "root_cause": "unknown: slow tier could not produce valid output",
    "proposed_fix": "Restart the service",
    "confidence": 0.0,
}

SYSTEM_PROMPT = """You are diagnosing an incident on a containerized Postgres database that
runs with hard memory and CPU limits and no swap. You are given facts measured by code and the
verdict of a fast triage step. Use only these facts; do not invent metrics, logs or events.
Reply with JSON only:
{"root_cause": "<one or two sentences>", "proposed_fix": "<one concrete remediation>",
 "confidence": <number 0-1>}
- oom_kill_delta > 0 means the kernel killed a process for exceeding the memory limit.
- cpu_share_of_limit is the average CPU use during the window as a fraction of cpu_limit_cores
  (1.0 = pinned at the limit); cpu_throttled_periods is how many scheduler periods the kernel
  throttled the container for hitting that limit. Fields that are absent were not measured.
- data_disk_percent_used is the fraction (0-1) of the database's data volume in use;
  write_failed=true means a real INSERT just failed, and write_error is the error it returned.
- running_query is the longest-running active query on the database when the window ended
  (pid, duration_seconds, query text); it is context, not proof that the query is the cause.
- If the facts do not pin down the cause, say so in root_cause and lower the confidence.
- proposed_fix is a proposal for a human or a later tool; keep it to a single action.
Available remediation types (choose the one that fits, in your own words): restart the
container; increase the container's memory limit; terminate a specific problematic query; or
flag the incident for human review if none of these applies."""


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
    for k in ("root_cause", "proposed_fix"):
        if not isinstance(obj[k], str) or not obj[k].strip():
            raise ValueError(f"{k} must be a non-empty string, got {obj[k]!r}")
    c = obj["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)):
        raise ValueError(f"confidence must be a number, got {c!r}")
    if not math.isfinite(c) or not 0 <= c <= 1:
        raise ValueError(f"confidence out of range [0,1]: {c!r}")
    return {"root_cause": obj["root_cause"].strip(),
            "proposed_fix": obj["proposed_fix"].strip(), "confidence": float(c)}


def ask_model(model, fast_result):
    sig = fast_result["signal"]
    facts = {
        # Only fields actually present in the signal: OOM-only records have no CPU keys.
        "signal": {k: sig[k] for k in ("oom_kill_delta", "window_seconds", "db_connect_ok",
                                       "db_connect_error", "cpu_limit_cores",
                                       "cpu_share_of_limit", "cpu_throttled_periods",
                                       "running_query", "data_disk_percent_used",
                                       "write_failed", "write_error")
                   if k in sig},
        "fast_tier_model_output": fast_result["model_raw"],
        "fast_tier_final_decision": fast_result["decision"],
        "fast_tier_decision_was_overridden_by_code": fast_result["overridden"],
    }
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=300, json={
        "model": model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Incident facts:\n" + json.dumps(facts)},
        ],
    })
    r.raise_for_status()
    return r.json()["message"]["content"]


def diagnose(fast_result, model=DEFAULT_MODEL, max_attempts=2):
    """Returns the validated diagnosis plus audit info. Never returns unvalidated output."""
    attempts = []
    for _ in range(max_attempts):  # first try + one retry
        try:
            raw = ask_model(model, fast_result)
        except requests.RequestException as e:
            attempts.append({"raw": None, "error": f"ollama request failed: {e}"})
            continue
        try:
            diagnosis = validate(raw)
        except ValueError as e:
            attempts.append({"raw": raw, "error": str(e)})
            continue
        attempts.append({"raw": raw, "error": None})
        return {"diagnosis": diagnosis, "source": "model", "model": model,
                "attempts": attempts}
    return {"diagnosis": dict(FALLBACK), "source": "fallback", "model": model,
            "attempts": attempts}

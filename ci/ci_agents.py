"""Fast and slow tiers for the CI domain: the same design as agents/fast_tier.py and
agents/slow_tier.py, pointed at pipeline failures.

Fast tier: small model -> {severity, escalate, confidence}, schema-validated in code (reject and
retry, never accept malformed output), then a deterministic FLOOR: a red pipeline always
escalates. Slow tier: larger model -> {root_cause, proposed_fix, confidence}, same validation.
Both only PROPOSE or CLASSIFY; nothing is executed here.
"""
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

import fast_tier
import slow_tier

CI_SEVERITY_FLOOR = 0.7   # a red pipeline blocks the team: never "ignore" it

FAST_PROMPT = """You triage CI pipeline results. You are given facts measured by code.
Do not invent facts. Reply with JSON only:
{"severity": <number 0-1>, "escalate": <true|false>, "confidence": <number 0-1>}
Guidance:
- conclusion is "success" or "failure". failed_step is the step that failed (null on success).
- error_class says what kind of error the failed step's log shows (null on success).
- conclusion "success" means no incident: low severity, escalate false.
- conclusion "failure" means the pipeline is red and needs triage: escalate true.
- confidence is how sure you are of this classification."""

SLOW_PROMPT = """You are diagnosing a failed CI pipeline for a small Python project. The
pipeline runs the steps fetch, deps, compile and test. You are given facts measured by code,
including an excerpt of the failed step's real log. Use only these facts; do not invent logs,
commits or events. Reply with JSON only:
{"root_cause": "<one or two sentences>", "proposed_fix": "<one concrete remediation>",
 "confidence": <number 0-1>}
- failed_step, exit_code and error_class/error_line say where and how the pipeline failed;
  log_excerpt is the end of that step's log.
- head_sha is the commit that was built. last_green_sha is the newest commit that passed before.
  head_is_last_green=true means this exact commit PASSED earlier; commits_since_green counts
  commits after the last green one and changed_files_since_green lists what they touched.
  earlier_failures_on_head counts earlier red runs of this same commit.
- Facts that are null could not be measured.
- If the facts do not pin down the cause, say so in root_cause and lower the confidence.
- proposed_fix is a proposal for a human or a later tool; keep it to a single action.
Available remediation types (choose the one that fits, in your own words): re-run the failed
pipeline; revert the most recent commit; fix the code or the tests by hand; install or restore
a missing dependency on the runner; or flag the incident for human review if none applies."""


def _chat(model, system, facts, schema, timeout):
    r = requests.post(f"{fast_tier.OLLAMA_URL}/api/chat", timeout=timeout, json={
        "model": model, "stream": False, "format": schema, "options": {"temperature": 0},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": facts}]})
    r.raise_for_status()
    body = r.json()
    fast_tier.record_usage(body)
    return body["message"]["content"]


def apply_floor(decision, signal):
    """A red pipeline always escalates, whatever the model said. Returns (decision, overridden)."""
    if signal.get("conclusion") != "failure":
        return decision, False
    final = dict(decision)
    final["escalate"] = True
    final["severity"] = max(decision["severity"], CI_SEVERITY_FLOOR)
    return final, final != decision


def classify(signal, model=fast_tier.DEFAULT_MODEL, max_attempts=2):
    facts = {k: signal.get(k) for k in ("conclusion", "failed_step", "exit_code", "error_class")}
    attempts, model_raw, source = [], None, "fallback"
    for _ in range(max_attempts):
        try:
            raw = _chat(model, FAST_PROMPT, "Signal:\n" + json.dumps(facts), fast_tier.SCHEMA, 120)
        except requests.RequestException as e:
            attempts.append({"raw": None, "error": f"ollama request failed: {e}"})
            continue
        try:
            model_raw = fast_tier.validate(raw)
        except ValueError as e:
            attempts.append({"raw": raw, "error": str(e)})
            continue
        attempts.append({"raw": raw, "error": None})
        source = "model"
        break
    if model_raw is None:   # no schema-valid output: fail safe toward a human
        decision, overridden = {"severity": 1.0, "escalate": True, "confidence": 0.0}, False
    else:
        decision, overridden = apply_floor(model_raw, signal)
    return {"signal": signal, "model": model, "model_raw": model_raw, "decision": decision,
            "overridden": overridden, "source": source, "attempts": attempts}


SLOW_KEYS = ("conclusion", "failed_step", "exit_code", "error_class", "error_line",
             "log_excerpt", "head_sha", "last_green_sha", "head_is_last_green",
             "commits_since_green", "changed_files_since_green", "earlier_failures_on_head")


def diagnose(fast_result, model=slow_tier.DEFAULT_MODEL, max_attempts=2):
    sig = fast_result["signal"]
    facts = {"signal": {k: sig.get(k) for k in SLOW_KEYS},
             "fast_tier_final_decision": fast_result["decision"]}
    attempts = []
    for _ in range(max_attempts):
        try:
            raw = _chat(model, SLOW_PROMPT, "Incident facts:\n" + json.dumps(facts),
                        slow_tier.SCHEMA, 300)
        except requests.RequestException as e:
            attempts.append({"raw": None, "error": f"ollama request failed: {e}"})
            continue
        try:
            diagnosis = slow_tier.validate(raw)
        except ValueError as e:
            attempts.append({"raw": raw, "error": str(e)})
            continue
        attempts.append({"raw": raw, "error": None})
        return {"diagnosis": diagnosis, "source": "model", "model": model, "attempts": attempts}
    return {"diagnosis": dict(slow_tier.FALLBACK), "source": "fallback", "model": model,
            "attempts": attempts}

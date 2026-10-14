"""Evaluation stage 2: baselines and ablations, replayed OFFLINE on the saved signals of the live
full-configuration run (eval/results/eval_runs.jsonl). Every configuration sees the identical 70
signals (60 faults + 10 quiet controls), so differences are due to the configuration alone.

Because nothing is re-injected or executed, these rows measure DECISION quality (detect, choose
the right action, whether policy would let a wrong action run). They do NOT measure fix success;
that is only measured for the full configuration (live, independent ground truth).

Configurations:
  single_7b                baseline (a): ONE always-on qwen2.5:7b call per signal decides
                           escalate + diagnose + propose; no fast tier, floor or knowledge.
                           Same mapper and policy downstream.
  fixed_rules              baseline (b): no LLM. Typical threshold alerts + restart remediation
                           (see fixed_rules()). Not tuned to the answers.
  no_floor                 full minus the deterministic floor (the fast model alone decides)
  no_knowledge             full minus the knowledge bundle (slow tier replayed without it)
  no_policy_gate           full, but every mapped action would run (no policy.yaml gate)
  health_only_verification full, but "verified" = a plain health check (no fault-specific check)

Usage: python eval/run_baselines.py [--configs a,b]     (resumable)
"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "eval"))

import action_mapper
import fast_tier
import slow_tier
from knowledge_experiment import GROUND_TRUTH
from tools import executor

RAW = ROOT / "eval" / "results" / "eval_runs.jsonl"
OUT = ROOT / "eval" / "results" / "eval_runs_offline.jsonl"
CONFIGS = ["single_7b", "fixed_rules", "no_floor", "no_knowledge", "no_policy_gate",
           "health_only_verification"]

SINGLE_NOTE = """
You are the ONLY decision step (there is no separate triage step). First decide whether anything
needs remediation at all: set "escalate" to false and keep root_cause/proposed_fix short
("none") if every fact is normal. Reply with JSON only:
{"escalate": <true|false>, "root_cause": "<...>", "proposed_fix": "<...>", "confidence": <0-1>}"""
SINGLE_SCHEMA = {
    "type": "object",
    "properties": {"escalate": {"type": "boolean"}, "root_cause": {"type": "string"},
                   "proposed_fix": {"type": "string"},
                   "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
    "required": ["escalate", "root_cause", "proposed_fix", "confidence"],
    "additionalProperties": False,
}
FACT_KEYS = ("oom_kill_delta", "window_seconds", "db_connect_ok", "db_connect_error",
             "cpu_limit_cores", "cpu_share_of_limit", "cpu_throttled_periods", "running_query",
             "data_disk_percent_used", "write_failed", "write_error", "db_connect_seconds",
             "pg_ready_inside", "container_state", "app_health_status", "app_container_state",
             "app_health_error", "last_deploy")


def fixed_rules(s):
    """Baseline (b): a typical static runbook, no model. Returns (escalate, action).
    Threshold alerts, restart-based remediation; disk-full is paged to a human because deleting
    data is never automatic. First matching rule wins."""
    def get(k):
        return s.get(k)
    app = get("app_health_status")
    if get("container_state") not in (None, "running"):
        return True, "RESTART_DEPENDENCY"            # database container is down: start it
    if (get("oom_kill_delta") or 0) > 0:
        return True, "RESTART_CONTAINER"             # OOM kill: restart
    if get("write_failed") is True or (get("data_disk_percent_used") or 0) >= 0.95:
        return True, "UNKNOWN_NEEDS_HUMAN"           # disk full: page a human
    if (get("cpu_share_of_limit") or 0) >= 0.8 and (get("cpu_throttled_periods") or 0) > 0:
        return True, "RESTART_CONTAINER"             # CPU pinned: restart
    if get("db_connect_ok") is False:
        return True, "RESTART_CONTAINER"             # cannot connect: restart
    if isinstance(app, int) and not isinstance(app, bool) and app != 200:
        return True, "RESTART_CONTAINER"             # app unhealthy: restart the service
    return False, None


def single_7b(signal, model):
    """Baseline (a): one model call does everything. Returns (decision dict, usage, seconds)."""
    sig = {k: signal[k] for k in FACT_KEYS if k in signal}
    prompt = slow_tier.build_system_prompt("last_deploy" in signal) + SINGLE_NOTE
    fast_tier.take_usage()
    t = time.perf_counter()
    for _ in range(2):
        try:
            r = requests.post(f"{fast_tier.OLLAMA_URL}/api/chat", timeout=300, json={
                "model": model, "stream": False, "format": SINGLE_SCHEMA, "options": {"temperature": 0},
                "messages": [{"role": "system", "content": prompt},
                             {"role": "user", "content": "Incident facts:\n" + json.dumps({"signal": sig})}]})
            r.raise_for_status()
            body = r.json()
            fast_tier.record_usage(body)
            obj = json.loads(body["message"]["content"])
            if (set(obj) == set(SINGLE_SCHEMA["required"]) and isinstance(obj["escalate"], bool)
                    and isinstance(obj["confidence"], (int, float)) and not isinstance(obj["confidence"], bool)
                    and 0 <= obj["confidence"] <= 1 and isinstance(obj["proposed_fix"], str)):
                return obj, fast_tier.take_usage(), round(time.perf_counter() - t, 2)
        except (requests.RequestException, ValueError, KeyError):
            pass
    # no valid output: fail safe toward a human, like the fast tier does
    return ({"escalate": True, "root_cause": "invalid output", "proposed_fix": "", "confidence": 0.0},
            fast_tier.take_usage(), round(time.perf_counter() - t, 2))


def base_row(src, config):
    keep = ("kind", "fault", "trial", "injector_effect", "fault_present_before", "fault_present_after")
    row = {k: src[k] for k in keep if k in src}
    row.update(config=config, exec_status="offline", exec_dry_run=True, overridden=False)
    return row


def decide(row, src, escalate, action, confidence, ttd, timings=None, usage=None):
    fault = src["fault"]
    row.update(final_escalated=escalate, mapped_action=action if escalate else None,
               record={"slow_tier": {"diagnosis": {"confidence": confidence}}},
               ttd_seconds=ttd, timings=timings or {}, usage=usage or {})
    row.setdefault("model_escalated", escalate)
    row["mapping_correct"] = bool(escalate and fault in GROUND_TRUTH and action in GROUND_TRUTH[fault])
    return row


def sampling_part(src):
    """Seconds spent sampling (the same for every configuration): full ttd minus its fast call."""
    return (src.get("ttd_seconds") or 0) - (src.get("timings") or {}).get("fast_seconds", 0) \
        if src["kind"] == "fault" else 5.0


def build(config, src, model, slow_model):
    row, rec = base_row(src, config), src["record"]
    samp = sampling_part(src)
    if config == "fixed_rules":
        esc, act = fixed_rules(rec["signal"])
        return decide(row, src, esc, act, 1.0, round(samp, 1))
    if config == "single_7b":
        obj, usage, secs = single_7b(rec["signal"], model)
        act = action_mapper.map_action(obj["proposed_fix"]).value if obj["escalate"] else None
        return decide(row, src, obj["escalate"], act, obj["confidence"], round(samp + secs, 1),
                      {"single_seconds": secs}, {"single": usage})
    if config == "no_floor":
        esc = bool(rec["model_raw"] and rec["model_raw"]["escalate"])
        row["model_escalated"] = esc
        return decide(row, src, esc, rec["mapped_action"], rec["slow_tier"]["diagnosis"]["confidence"]
                      if rec.get("slow_tier") else 0.0, src.get("ttd_seconds"))
    if config == "no_knowledge":
        if not rec["decision"]["escalate"]:
            return decide(row, src, False, None, 0.0, src.get("ttd_seconds"))
        fast = {k: rec[k] for k in ("signal", "model_raw", "decision", "overridden")}
        t = time.perf_counter()
        out = slow_tier.diagnose(fast, slow_model, knowledge=None)
        secs = round(time.perf_counter() - t, 2)
        d = out["diagnosis"]
        return decide(row, src, True, action_mapper.map_action(d["proposed_fix"]).value,
                      d["confidence"], src.get("ttd_seconds"), {"slow_seconds": secs})
    if config == "no_policy_gate":
        row.update(policy_gate=False, model_escalated=src.get("model_escalated"))
        return decide(row, src, src["final_escalated"], src["mapped_action"],
                      rec["slow_tier"]["diagnosis"]["confidence"] if rec.get("slow_tier") else 0.0,
                      src.get("ttd_seconds"))
    if config == "health_only_verification":
        row = {**src, "config": config}
        row.pop("record", None)
        row["record"] = rec
        if src.get("exec_status") == "executed":
            row["verified"] = src["plain_health_verified"]
            row["verifier_false_pass"] = src["health_only_false_pass"]
        return row
    raise ValueError(config)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--configs", default=",".join(CONFIGS))
    p.add_argument("--model", default=slow_tier.DEFAULT_MODEL)
    a = p.parse_args()
    rows = [json.loads(l) for l in RAW.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if "harness_error" not in r]
    done = set()
    if OUT.exists():
        done = {(r["config"], r["kind"], r["fault"], r["trial"])
                for r in map(json.loads, OUT.read_text().splitlines())}
    for config in a.configs.split(","):
        n = 0
        for src in rows:
            key = (config, src["kind"], src["fault"], src["trial"])
            if key in done:
                continue
            row = build(config, src, a.model, a.model)
            with OUT.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            n += 1
        print(f"{config}: {n} rows", flush=True)


if __name__ == "__main__":
    main()

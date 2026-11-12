"""The OpsMind v2 incident graph (LangGraph).

  sense -> triage --(not escalated)--------------------------------------------> close
             | escalated
             v
           route (code) --Send--> specialist x N (parallel agents, read-only tools)
                                        |
                                        v
                               commander (agent: plan of 1-2 steps)
                                        |
                                        v
                               critic (agent: approve / revise, one revision allowed)
                                        |
                                        v
                               gate (code: mapper, evidence gate, policy, governor)
                                 |                    |
                                 | all automatic      | some step needs a person (hitl on)
                                 v                    v
                                act  <---------  approval (interrupt: waits for a human)
                                 |
                                 v
                              verify (code) -> close (record, knowledge, hand-off)

Agents: triage (3B + code floor), database / network / application / cache specialists,
commander, critic (7B). Everything that changes the system or the record is code.
Every node emits events (opsgraph/events.py) that the console streams live.
"""
import operator
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from . import ROOT  # noqa: F401  (sets sys.path)
from . import specialists
from .events import Emitter
import action_mapper
import fast_tier
import investigator
import slow_tier
import supervisor
from tools import deploy, escalation, executor, forensics, governor, knowledge
from verify import health_check

PG, APP, CACHE = "opsmind-postgres", "opsmind-app", "opsmind-redis"
MAX_REVISIONS = 2

# Which component an action acts on. The evidence gate lets an action run automatically only if
# a specialist reported that component FAULTY with grounded evidence.
ACTION_COMPONENTS = {
    "RESTART_DEPENDENCY": {"database", "cache"},
    "RESTART_CONTAINER": {"database"},
    "TERMINATE_QUERY": {"database"},
    "RECONNECT_NETWORK": {"network"},
    "ROLLBACK_CONFIG": {"application"},
}

COMMANDER_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "steps": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
            "type": "object",
            "properties": {"component": {"type": "string",
                                         "enum": list(specialists.SPECIALISTS) + ["none"]},
                           "fix": {"type": "string"}},
            "required": ["component", "fix"]}},
        "confidence": {"type": "number"},
    },
    "required": ["root_cause", "steps", "confidence"],
}

CRITIC_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["approve", "revise"]},
                   "unexplained_facts": {"type": "array", "items": {"type": "string"}},
                   "reason": {"type": "string"}},
    "required": ["verdict", "unexplained_facts", "reason"],
}

COMMANDER_PROMPT = """You are the incident commander. Specialists investigated their own
components and reported findings; a finding marked grounded=false is not backed by evidence.
Decide the root cause and a remediation plan of ONE step, or TWO steps only if two different
components are faulty. Each step names the component it fixes and ONE remediation in your own
words. Remediations that exist: restart a dependency container (the database or the cache) that
is not running; restart the database container; increase the database container's memory limit;
increase its CPU limit; terminate a specific problematic query; reconnect a container to its
Docker network; roll back the web app's config to the last known-good deploy; or flag the
incident for human review if none of these fixes the actual cause. A component that fails
only because another one is broken (the web app failing because a dependency is down) is a
symptom: fix the cause. Every component that a grounded finding shows faulty as a CAUSE needs a
step. Use only the facts and findings given. Reply with JSON only: {"root_cause": "...", "steps": [{"component":
"database|network|application|cache|none", "fix": "..."}], "confidence": 0-1}."""

CRITIC_PROMPT = """You are the reviewer on an incident response team. Check the commander's
plan against the measured abnormal facts and the specialists' findings. Approve only if the
root cause explains every abnormal fact and each step fixes a component that a grounded
finding shows to be faulty. Otherwise ask for a revision and list the facts left unexplained.
Do not invent facts. Reply with JSON only: {"verdict": "approve"|"revise",
"unexplained_facts": [...], "reason": "..."}."""


class State(TypedDict, total=False):
    run_id: str
    cfg: dict
    signal: dict
    triage: dict
    route: list
    specialist: str                       # set only inside a Send payload
    findings: Annotated[list, operator.add]
    knowledge_context: dict
    diagnosis: dict
    critiques: Annotated[list, operator.add]
    revisions: int
    plan: list
    approval: dict
    executions: list
    verification: dict
    outcome: dict


def _emitter(state):
    return Emitter(state["run_id"], state["cfg"].get("events_dir"))


def _db(cfg):
    return cfg["db"]


def _toolbox(cfg):
    return investigator.Toolbox(_db(cfg), cfg["app_port"], deploy.recent_deploy_facts)


# ------------------------------------------------------------------ nodes

def sense(state):
    em = _emitter(state)
    cfg = state["cfg"]
    em.node("sense", "start")
    if state.get("signal") is not None:          # a signal can be supplied (eval / replay)
        sig = state["signal"]
    else:
        sig = fast_tier.collect_signal(PG, window=cfg.get("window", 5), app=APP,
                                       app_port=cfg["app_port"], cache=CACHE, **_db(cfg))
        supervisor.attach_running_query(sig, _db(cfg))
        supervisor.attach_deploy_facts(sig)
    em.emit(type="signal", agent="sense",
            data={"signal": sig, "abnormal": investigator.abnormal_facts(sig)})
    em.node("sense", "end")
    return {"signal": sig}


def triage(state):
    em = _emitter(state)
    em.node("triage", "start")
    result = fast_tier.classify(state["signal"], state["cfg"].get("fast_model",
                                                                  fast_tier.DEFAULT_MODEL))
    t = {"model_decision": result.get("model_raw"), "decision": result["decision"],
         "overridden": result["overridden"], "source": result["source"]}
    em.emit(type="decision", agent="triage",
            data={"escalate": result["decision"]["escalate"],
                  "severity": result["decision"].get("severity"),
                  "overridden_by_floor": result["overridden"]})
    em.node("triage", "end")
    return {"triage": t}


def after_triage(state):
    return "route" if state["triage"]["decision"]["escalate"] else "close"


def route(state):
    """Deterministic: which specialists to dispatch, from the measured facts. Auditable."""
    em = _emitter(state)
    em.node("route", "start")
    s = state["signal"]
    picks = []
    app_bad = s.get("app_health_status") != 200 or s.get("app_container_state") not in (None, "running")
    db_signs = ((s.get("oom_kill_delta") or 0) > 0 or (s.get("cpu_share_of_limit") or 0) >= 0.8
                or (s.get("data_disk_percent_used") or 0) >= 0.9 or s.get("write_failed")
                or s.get("container_state") not in (None, "running")
                or s.get("db_connect_ok") is False or s.get("app_db_ok") is False)
    if db_signs:
        picks.append("database")
    if s.get("db_connect_ok") is False and s.get("pg_ready_inside") is True:
        picks.append("network")
    err = (s.get("app_health_error") or "").lower()
    if "resolve" in err or "translate host" in err or "name or service" in err:
        if "network" not in picks:
            picks.append("network")
    if app_bad or (s.get("last_deploy") or {}).get("status_when_deployed") == "bad":
        picks.append("application")
    if s.get("redis_container_state") not in (None, "running") or s.get("app_redis_ok") is False:
        picks.append("cache")
    if not picks:
        picks = ["database", "application"]
    kctx = knowledge.context_for(s) if state["cfg"].get("knowledge", True) else None
    em.emit(type="route", agent="router", data={"specialists": picks,
                                                "precedent_shown": kctx is not None})
    em.node("route", "end")
    return {"route": picks, "knowledge_context": kctx}


def fan_out(state):
    return [Send("specialist", {"run_id": state["run_id"], "cfg": state["cfg"],
                                "signal": state["signal"], "specialist": name})
            for name in state["route"]]


def specialist(state):
    em = _emitter(state)
    name = state["specialist"]
    em.node(f"specialist:{name}", "start")
    f = specialists.investigate(name, state["signal"], _toolbox(state["cfg"]),
                                state["cfg"].get("slow_model", slow_tier.DEFAULT_MODEL),
                                emit=lambda **e: em.emit(**e))
    em.node(f"specialist:{name}", "end")
    return {"findings": [f]}


def _brief_findings(findings):
    return [{k: f.get(k) for k in ("component", "status", "summary", "grounded", "evidence",
                                   "confidence", "note")} for f in findings]


def commander(state):
    em = _emitter(state)
    em.node("commander", "start")
    facts = {"abnormal_facts": investigator.abnormal_facts(state["signal"]),
             "findings": _brief_findings(state["findings"])}
    if state.get("knowledge_context"):
        facts["past_verified_incidents_of_this_type"] = state["knowledge_context"]
    if state.get("critiques"):
        facts["reviewer_objection_to_your_last_plan"] = state["critiques"][-1]
    d = None
    for _ in range(2):          # schema-validated, one retry
        try:
            msg = specialists._chat(state["cfg"].get("slow_model", slow_tier.DEFAULT_MODEL),
                                    [{"role": "system", "content": COMMANDER_PROMPT},
                                     {"role": "user", "content": "Facts:\n" + _dumps(facts)}],
                                    fmt=COMMANDER_SCHEMA)
            d = validate_plan(investigator.extract_json(msg.get("content") or ""))
            break
        except Exception as e:  # invalid output or Ollama error: retry, then fall back
            em.emit(type="rejected_output", agent="commander", data={"error": str(e)[:200]})
    if d is None:
        d = {"root_cause": "commander could not produce a valid plan",
             "steps": [{"component": "none", "fix": "Flag the incident for human review"}],
             "confidence": 0.0, "fallback": True}
    em.emit(type="diagnosis", agent="commander", data=d)
    em.node("commander", "end")
    return {"diagnosis": d}


def validate_plan(raw):
    import json
    obj = json.loads(raw)
    if not isinstance(obj, dict) or not str(obj.get("root_cause", "")).strip():
        raise ValueError("root_cause missing")
    steps = obj.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 2:
        raise ValueError("steps must be a list of 1-2 items")
    clean = []
    for st in steps:
        if not isinstance(st, dict) or not str(st.get("fix", "")).strip():
            raise ValueError("each step needs a fix")
        comp = st.get("component") if st.get("component") in (*specialists.SPECIALISTS, "none") else "none"
        clean.append({"component": comp, "fix": str(st["fix"]).strip()})
    c = obj.get("confidence")
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
        raise ValueError(f"confidence must be in [0,1], got {c!r}")
    return {"root_cause": str(obj["root_cause"]).strip(), "steps": clean, "confidence": float(c)}


CAUSE_COMPONENTS = ("database", "network", "cache")
STEP_ORDER = {"RESTART_DEPENDENCY": 0, "RECONNECT_NETWORK": 0, "RESTART_CONTAINER": 1,
              "TERMINATE_QUERY": 1, "ROLLBACK_CONFIG": 2}


def app_config_is_a_cause(signal):
    """The web app is a CAUSE only while a config that differs from the last good deploy is
    live. A deploy merely labelled "bad" is not enough: during the first evaluation attempt a
    correct rollback had been labelled bad (its health check ran while the cache was down) and
    every later incident was wrongly treated as a config problem."""
    return bool((signal.get("last_deploy") or {}).get("changed_vs_last_known_good"))


def measured_causes(signal):
    """Faults that CODE measured directly, by component, with the measurement. These, not the
    agents' opinions, decide what a plan must cover. (v2a required coverage of every component an
    agent called faulty; during a network partition the database agent, unable to connect,
    called the database faulty, and plans looped until a person was needed.)"""
    s, out = signal, {}
    if s.get("container_state") not in (None, "running"):
        out["database"] = f"database container state is {s['container_state']!r}"
    elif (s.get("oom_kill_delta") or 0) > 0:
        out["database"] = f"the kernel OOM-killed a database process ({s['oom_kill_delta']}x)"
    if s.get("redis_container_state") not in (None, "running"):
        out["cache"] = f"cache container state is {s['redis_container_state']!r}"
    if s.get("db_connect_ok") is False and s.get("pg_ready_inside") is True \
            and "fatal" not in (s.get("db_connect_error") or "").lower():
        out["network"] = "the database is ready inside its container but cannot be reached"
    if app_config_is_a_cause(s):
        out["application"] = ("the live config differs from the last good deploy: "
                              f"{s['last_deploy']['changed_vs_last_known_good']}")
    return out


def coverage_objections(signal, findings, plan):
    """Deterministic review of a plan: every directly measured cause needs a step for its
    component, and each step's action must act on the component it claims to fix."""
    causes = measured_causes(signal)
    covered = {st["component"] for st in plan["steps"]}
    out = [f"{c} needs a step: measured, {fact}" for c, fact in sorted(causes.items())
           if c not in covered]
    for st in plan["steps"]:
        action = action_mapper.map_action(st["fix"]).value
        comps = ACTION_COMPONENTS.get(action)
        if comps is not None and st["component"] in CAUSE_COMPONENTS + ("application",) \
                and st["component"] not in comps:
            fact = causes.get(st["component"])
            out.append(f"the step for {st['component']} ('{st['fix']}') is a {action} action, "
                       f"which acts on {'/'.join(sorted(comps))}, not on {st['component']}"
                       + (f" (measured: {fact})" if fact else ""))
    return out


def critic(state):
    em = _emitter(state)
    em.node("critic", "start")
    code_objections = coverage_objections(state["signal"], state["findings"], state["diagnosis"])
    if code_objections:
        em.emit(type="code_check", agent="critic", data={"objections": code_objections})
    facts = {"abnormal_facts": investigator.abnormal_facts(state["signal"]),
             "findings": _brief_findings(state["findings"]), "plan": state["diagnosis"]}
    verdict = None
    try:
        msg = specialists._chat(state["cfg"].get("slow_model", slow_tier.DEFAULT_MODEL),
                                [{"role": "system", "content": CRITIC_PROMPT},
                                 {"role": "user", "content": "Review:\n" + _dumps(facts)}],
                                fmt=CRITIC_SCHEMA)
        import json
        v = json.loads(investigator.extract_json(msg.get("content") or ""))
        if v.get("verdict") in ("approve", "revise"):
            verdict = {"verdict": v["verdict"], "reason": str(v.get("reason", ""))[:400],
                       "unexplained_facts": [str(x) for x in (v.get("unexplained_facts") or [])][:6]}
    except Exception as e:
        em.emit(type="rejected_output", agent="critic", data={"error": str(e)[:200]})
    if verdict is None:     # a critic that cannot answer does not approve
        verdict = {"verdict": "revise", "reason": "critic gave no valid verdict",
                   "unexplained_facts": []}
    # v2b: the critic MODEL is advisory. In v2a it vetoed correct plans with objections that
    # were not true (3 times) and approved a plan the code rejected; only CODE objections send a
    # plan back or block automation. The model's view is kept and shown as a warning.
    verdict["model_verdict"] = verdict["verdict"]
    if code_objections:
        verdict.update(verdict="revise", reason="code check: " + "; ".join(code_objections)
                       + (f" (model reviewer said: {verdict['reason']})" if verdict.get("reason") else ""))
    else:
        verdict["verdict"] = "approve"
    verdict["code_objections"] = code_objections
    verdict["round"] = state.get("revisions", 0)
    em.emit(type="critique", agent="critic", data=verdict)
    em.node("critic", "end")
    upd = {"critiques": [verdict]}
    if verdict["verdict"] == "revise":
        upd["revisions"] = state.get("revisions", 0) + 1
    return upd


def after_critic(state):
    last = state["critiques"][-1]
    if last["verdict"] == "revise" and last["round"] < MAX_REVISIONS:
        return "commander"
    return "gate"


def gate(state):
    """Code decides, per step: action (mapper + guard), evidence, policy, governor."""
    em = _emitter(state)
    em.node("gate", "start")
    cfg, sig, d = state["cfg"], state["signal"], state["diagnosis"]
    policy = executor.load_policy()
    faulty = {f["component"] for f in state["findings"] if f["status"] == "faulty" and f["grounded"]}
    last = state["critiques"][-1]
    unresolved = bool(last.get("code_objections"))
    plan = []
    # Code decides the order: lower layers first (a stopped dependency or a detached network
    # before a config rollback), so each step is judged on a system whose foundations work.
    # Found live: rolling back the config while the cache was still down made the rollback's
    # own health check report "failed" although the rollback itself was correct.
    order = sorted(range(len(d["steps"])), key=lambda i: STEP_ORDER.get(
        action_mapper.map_action(d["steps"][i]["fix"]).value, 1))
    for i, st in ((k, d["steps"][k]) for k in order):
        action, note = action_mapper.guard_restart_target(action_mapper.map_action(st["fix"]), sig)
        a = action.value
        checks = [{"check": "mapper", "ok": a != "UNKNOWN_NEEDS_HUMAN",
                   "detail": f"'{st['fix']}' -> {a}" + (f" ({note})" if note else "")}]
        allowed, why = executor.check_policy(a, d["confidence"], policy)
        checks.append({"check": "policy", "ok": allowed, "detail": why})
        comps = ACTION_COMPONENTS.get(a)
        if comps is not None:
            ev_ok = bool(comps & faulty)
            checks.append({"check": "evidence", "ok": ev_ok, "detail":
                           f"grounded faulty finding for {sorted(comps & faulty)}" if ev_ok else
                           f"no grounded faulty finding for {sorted(comps)}"})
        checks.append({"check": "review", "ok": not unresolved,
                       "detail": "code review passed" if not unresolved
                       else "code review objection unresolved: " + "; ".join(last["code_objections"])})
        if last.get("model_verdict") == "revise":
            checks.append({"check": "critic", "ok": True, "advisory": True,
                           "detail": "advisory: critic model objected: " + last.get("reason", "")[:200]})
        plan_ex = executor.execute(a, d["confidence"], PG, True, db=_db(cfg),
                                   dependencies=[PG, CACHE], app_port=cfg["app_port"],
                                   policy=_human_ok(policy, a))
        has_target = plan_ex["status"] == "would_execute"
        checks.append({"check": "target", "ok": has_target,
                       "detail": plan_ex.get("reason") or plan_ex["status"]})
        targets = plan_ex.get("targets") or [PG]
        if cfg.get("governor", True) and not cfg.get("dry_run", True) and has_target:
            g_ok, g_why, _ = governor.check(a, targets)
            checks.append({"check": "governor", "ok": g_ok, "detail": g_why})
        executable = a in ACTION_COMPONENTS          # an executor exists for it
        auto = executable and all(c["ok"] for c in checks)
        governor_refused = any(c["check"] == "governor" and not c["ok"] for c in checks)
        nothing_to_do = executable and not has_target   # e.g. dependency already running
        # refused: the code knows acting would be wrong or pointless; needs_human: a person
        # decides (or performs it by hand when no executor exists).
        decision = ("auto" if auto else
                    "refused" if governor_refused or nothing_to_do else "needs_human")
        plan.append({"step": i, "component": st["component"], "fix": st["fix"], "action": a,
                     "targets": targets, "checks": checks, "decision": decision})
        em.emit(type="gate", agent="gate", data=plan[-1])
    em.node("gate", "end")
    return {"plan": plan}


def _human_ok(policy, action):
    """Policy copy in which a person has approved this one action (used for planning and for
    human-approved execution; the governor and the executor's own guards still apply)."""
    p = dict(policy)
    p[action] = {"auto_approve": True, "min_confidence": 0.0}
    return p


def after_gate(state):
    if any(s["decision"] == "needs_human" for s in state["plan"]) and state["cfg"].get("hitl"):
        return "approval"
    return "act"


def approval(state):
    em = _emitter(state)
    em.node("approval", "start")
    pending = [s for s in state["plan"] if s["decision"] == "needs_human"]
    em.emit(type="awaiting_human", agent="approval", data={"steps": pending})
    answer = interrupt({"run_id": state["run_id"], "steps": pending})
    approved = set(answer.get("approve", [])) if isinstance(answer, dict) else set()
    plan = []
    for s in state["plan"]:
        if s["decision"] == "needs_human":
            s = {**s, "decision": "human_approved" if s["step"] in approved else "human_rejected"}
        plan.append(s)
    em.emit(type="human_decision", agent="approval",
            data={"approved": sorted(approved), "by": (answer or {}).get("by", "operator")})
    em.node("approval", "end")
    return {"plan": plan, "approval": answer if isinstance(answer, dict) else {}}


def act(state):
    em = _emitter(state)
    em.node("act", "start")
    cfg = state["cfg"]
    policy = executor.load_policy()
    out = []
    for s in state["plan"]:
        if s["decision"] not in ("auto", "human_approved"):
            out.append({"step": s["step"], "action": s["action"], "status": "not_run",
                        "reason": s["decision"]})
            continue
        if s["action"] not in ACTION_COMPONENTS:     # approved, but only a person can do it
            out.append({"step": s["step"], "action": s["action"], "status": "manual_action_required",
                        "reason": "approved by a person; no executor exists for this action"})
            em.emit(type="action", agent="executor", data=out[-1])
            continue
        if not cfg.get("dry_run", True) and cfg.get("governor", True):
            g_ok, g_why, _ = governor.check(s["action"], s["targets"])
            if not g_ok:
                out.append({"step": s["step"], "action": s["action"],
                            "status": "blocked_by_governor", "reason": g_why})
                em.emit(type="action", agent="executor", data=out[-1])
                continue
        before = _topology()
        snap = forensics.snapshot(set(s["targets"]) | {PG}) if not cfg.get("dry_run", True) else None
        ex = executor.execute(s["action"], state["diagnosis"]["confidence"], PG,
                              cfg.get("dry_run", True), db=_db(cfg), dependencies=[PG, CACHE],
                              app_port=cfg["app_port"], policy=_human_ok(policy, s["action"]))
        rec = {"step": s["step"], **ex, "pre_action_snapshot": snap,
               "topology_before": before, "topology_after": _topology()}
        out.append(rec)
        em.emit(type="action", agent="executor",
                data={k: v for k, v in rec.items() if k != "pre_action_snapshot"})
    em.node("act", "end")
    return {"executions": out}


def verify_node(state):
    em = _emitter(state)
    em.node("verify", "start")
    cfg = state["cfg"]
    ran = [e for e in state["executions"] if e.get("status") in ("executed", "failed")]
    v = None
    if any(e.get("status") == "executed" for e in ran):
        actions = [e["action"] for e in ran]       # every attempted step must hold
        expected = "config" if "ROLLBACK_CONFIG" in actions else \
            supervisor.EXPECTED_SIGNAL.get(actions[-1])
        if len(ran) > 1:            # several steps: the whole system must be healthy again
            expected = "config" if "ROLLBACK_CONFIG" in actions else "app"
        v = health_check.verify(PG, **_db(cfg), expected_signal=expected, app_port=cfg["app_port"])
        v["expected_signal"] = expected
        em.emit(type="verification", agent="verifier", data=v)
    em.node("verify", "end")
    return {"verification": v}


def close(state):
    """Record the run (one runs.jsonl line per step, so the governor and dashboard see it),
    write knowledge for verified single-step live fixes, queue hand-offs."""
    em = _emitter(state)
    em.node("close", "start")
    cfg = state["cfg"]
    v = state.get("verification")
    execs = state.get("executions") or []
    plan = state.get("plan") or []
    escalated = state["triage"]["decision"]["escalate"]
    if not escalated:
        status = "no_incident"
    elif v and v.get("verified"):
        if any(e.get("status") == "failed" for e in execs):
            status = "resolved_but_a_step_reported_failure"
        elif all(s["decision"] in ("auto", "human_approved") for s in plan):
            status = "resolved"
        else:
            status = "partly_resolved_needs_human"
    elif any(e.get("status") == "would_execute" for e in execs):
        status = "dry_run_planned"
    elif any(e.get("status") == "executed" for e in execs):
        status = "fix_ran_not_verified"
    else:
        status = "handed_to_human"
    records = []
    steps = plan or [None]
    for s in steps:
        ex = next((e for e in execs if s and e["step"] == s["step"]), None)
        rec = {"run_id": f"{state['run_id']}" + (f"-s{s['step']}" if s and len(plan) > 1 else ""),
               "graph_run_id": state["run_id"], "engine": "opsgraph",
               "timestamp": datetime.now(timezone.utc).isoformat(),
               "action": "escalate_slow_tier" if escalated else "no_action",
               "signal": state["signal"], "decision": state["triage"]["decision"],
               "model_raw": state["triage"].get("model_decision"),
               "overridden": state["triage"]["overridden"],
               "slow_tier": None if not s else {"diagnosis": {
                   "root_cause": state["diagnosis"]["root_cause"], "proposed_fix": s["fix"],
                   "confidence": state["diagnosis"]["confidence"]}, "source": "opsgraph"},
               "knowledge_context": state.get("knowledge_context"),
               "mapped_action": s["action"] if s else None,
               "execution": None if not s else (
                   {k: v2 for k, v2 in ex.items() if k not in ("topology_before", "topology_after",
                                                               "pre_action_snapshot")}
                   if ex and ex.get("status") != "not_run" else
                   {"action": s["action"], "container": PG, "dry_run": cfg.get("dry_run", True),
                    "status": "blocked_pending_human_approval" if s["decision"] in
                    ("needs_human", "human_rejected") else
                    ("blocked_by_governor" if s["decision"] == "refused" and any(
                        c["check"] == "governor" and not c["ok"] for c in s["checks"]) else
                     "no_target" if s["decision"] == "refused" else "not_run"),
                    "reason": "; ".join(c["detail"] for c in s["checks"] if not c["ok"])}),
               "verification": v if ex and ex.get("status") == "executed" else None,
               "pre_action_snapshot": ex.get("pre_action_snapshot") if ex else None}
        records.append(rec)
    if cfg.get("record", True):
        for rec in records:
            supervisor.write_trajectory(rec)
    notes = []
    if cfg.get("record", True) and not cfg.get("dry_run", True):
        if len(plan) == 1 and records[0]["verification"] and records[0]["verification"].get("verified"):
            try:
                st, detail = knowledge.record_incident(records[0])
                notes.append(f"knowledge: {st} ({detail})")
            except Exception as e:
                notes.append(f"knowledge: failed ({type(e).__name__})")
        for rec in records:
            entry = escalation.escalate(rec)
            if entry:
                notes.append(f"handed to a human: {entry['reason']}")
    outcome = {"status": status, "notes": notes, "finished": datetime.now(timezone.utc).isoformat()}
    em.emit(type="outcome", agent="close", data=outcome)
    em.node("close", "end")
    return {"outcome": outcome}


def _topology():
    """Container states and app health right now (for before/after views)."""
    out = {c: fast_tier.container_state(c) for c in (PG, APP, CACHE)}
    try:
        status, _, body = fast_tier.probe_app(5050, timeout=3)
        out["app_health"] = {"status": status, **{k: body.get(k) for k in ("db_ok", "redis_ok")}}
    except Exception:
        out["app_health"] = None
    try:
        out["live_config"] = deploy.read_live()
    except Exception:
        out["live_config"] = None
    return out


def _dumps(obj):
    import json
    return json.dumps(obj, default=str)


# ------------------------------------------------------------------ graph

def build(checkpointer=None):
    g = StateGraph(State)
    for name, fn in (("sense", sense), ("triage", triage), ("route", route),
                     ("specialist", specialist), ("commander", commander), ("critic", critic),
                     ("gate", gate), ("approval", approval), ("act", act),
                     ("verify", verify_node), ("close", close)):
        g.add_node(name, fn)
    g.add_edge(START, "sense")
    g.add_edge("sense", "triage")
    g.add_conditional_edges("triage", after_triage, ["route", "close"])
    g.add_conditional_edges("route", fan_out, ["specialist"])
    g.add_edge("specialist", "commander")
    g.add_edge("commander", "critic")
    g.add_conditional_edges("critic", after_critic, ["commander", "gate"])
    g.add_conditional_edges("gate", after_gate, ["approval", "act"])
    g.add_edge("approval", "act")
    g.add_edge("act", "verify")
    g.add_edge("verify", "close")
    g.add_edge("close", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def default_cfg(**over):
    e = fast_tier.load_env()
    cfg = {"db": dict(host="localhost", port=int(e.get("POSTGRES_HOST_PORT", 5433)),
                      user=e["POSTGRES_USER"], password=e["POSTGRES_PASSWORD"],
                      dbname=e["POSTGRES_DB"]),
           "app_port": int(e.get("APP_HOST_PORT", 5050)), "window": 5, "dry_run": True,
           "hitl": False, "governor": True, "knowledge": True, "record": True,
           "fast_model": fast_tier.DEFAULT_MODEL, "slow_model": slow_tier.DEFAULT_MODEL}
    cfg.update(over)
    return cfg


def new_run_id():
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

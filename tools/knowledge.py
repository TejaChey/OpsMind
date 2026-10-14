"""OKF knowledge + provenance layer (Open Knowledge Format v0.2: markdown + YAML frontmatter).

The bundle lives in knowledge/. It is written ONLY by this code, from facts already recorded in
a trajectory line and in policy.yaml. No model is ever asked to write or summarise anything
here, so the knowledge base can never contain a model-invented fact.

  knowledge/
    index.md                  reserved: directory listing (carries okf_version)
    log.md                    reserved: dated history of updates, newest first
    services/*.md             type: Service        (what OpsMind watches)
    faults/*.md               type: Fault Type     (signals, correct fix, policy, verified outcomes)
    runbooks/*.md             type: Playbook       (the steps OpsMind follows for that fault)

After a VERIFIED real incident, record_incident() adds the run to the matching fault node:
a `sources` entry citing the run_id (in trajectories/runs.jsonl), a `verified` entry by the
automated check (so the trust tier is "machine-confirmed", never "human-reviewed"), and one
row in the outcomes table with a footnote keyed to that source id (spec section 5.1).

Refused (nothing written): harness runs (HARNESS_TEST), dry-runs, actions that did not execute,
and runs whose code-decided verification is not True. Writing the same run twice is a no-op.

Usage (from the repo root):
    python tools/knowledge.py seed        create/refresh nodes for all 6 faults + services
    python tools/knowledge.py backfill    record every verified real run in runs.jsonl
    python tools/knowledge.py validate    check the bundle against the OKF v0.2 conformance rules
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BUNDLE = ROOT / "knowledge"
RUNS_FILE = ROOT / "trajectories" / "runs.jsonl"
OKF_VERSION = "0.2"
WRITER = "opsmind-knowledge-writer/0.1"          # actor convention: <producer>/<version>
VERIFIER = "process:opsmind-verify"              # automated code check => machine-confirmed
RESERVED = {"index.md", "log.md"}

# Static, code-owned facts per fault type. `signals`/`floor` describe agents/fast_tier.py;
# `correct_fix` is the ground-truth remediation used for evaluation (CLAUDE.md).
FAULTS = {
    "oom": {
        "title": "Out-of-memory kill",
        "description": "The kernel killed a process in the database container for exceeding its memory limit.",
        "services": ["postgres"],
        "signals": ["`oom_kill_delta` > 0 (cgroup `memory.events`, read before and after a window)"],
        "floor": "any `oom_kill_delta` > 0 forces escalate=true and severity >= 0.8",
        "correct_fix": "INCREASE_MEMORY_LIMIT (a human decides; capacity change)",
        "actions": ["INCREASE_MEMORY_LIMIT", "RESTART_CONTAINER"],
        "findings": ["Docker's `OOMKilled` flag is sticky and the container can stay 'healthy' during a real incident, so the cgroup counter is the evidence."],
        "steps": ["Read the cgroup `oom_kill` delta.", "Escalate (floor) and diagnose.", "Propose raising the memory limit; policy sends it to a human."],
    },
    "cpu-spike": {
        "title": "CPU spike",
        "description": "The database container is pinned at its CPU limit and throttled by the kernel.",
        "services": ["postgres"],
        "signals": ["`cpu_share_of_limit` >= 0.8", "`cpu_throttled_periods` > 0 (throttling is the trustworthy evidence)"],
        "floor": "share >= 0.8 AND throttled periods > 0 forces escalate=true and severity >= 0.8",
        "correct_fix": "TERMINATE_QUERY (end the runaway query; small and reversible)",
        "actions": ["TERMINATE_QUERY", "RESTART_CONTAINER"],
        "findings": ["The slow tier consistently proposed 'increase the CPU limit' instead of ending the query, even with full context and an action menu; that routes to UNKNOWN_NEEDS_HUMAN and is recorded as a finding, not forced."],
        "steps": ["Sample CPU share and throttled periods.", "Escalate (floor) and diagnose.", "End the longest-running query (verified: CPU share falls below 0.5)."],
    },
    "disk-full": {
        "title": "Disk full",
        "description": "The database's data volume is full and writes fail with 'No space left on device'.",
        "services": ["postgres"],
        "signals": ["`data_disk_percent_used` >= 0.95", "`write_failed` is true (a real 64 KB INSERT probe failed)"],
        "floor": "write_failed True OR usage >= 0.95 forces escalate=true and severity >= 0.8",
        "correct_fix": "INCREASE_DISK or FREE_DISK_SPACE (human only; deleting data is never automatic)",
        "actions": ["INCREASE_DISK", "FREE_DISK_SPACE"],
        "findings": ["A restart does not free the volume, so no safe automatable disk action exists; leaving it to a human is the intended result.", "The model's compound proposal 'increase the volume or remove data' maps to UNKNOWN_NEEDS_HUMAN."],
        "steps": ["Read volume usage and run the write probe.", "Escalate (floor) and diagnose.", "Hand to a human; verification needs the write probe to succeed AND usage < 0.90."],
    },
    "network-partition": {
        "title": "Network partition",
        "description": "The database process is fine but the container is cut off from its network, so clients cannot connect.",
        "services": ["postgres"],
        "signals": ["`db_connect_ok` is false from the host", "`pg_ready_inside` is true (pg_isready run inside the container)"],
        "floor": "db_connect_ok False AND pg_ready_inside True forces escalate=true and severity >= 0.8",
        "correct_fix": "RECONNECT_NETWORK (re-attach the container, restoring its DNS aliases)",
        "actions": ["RECONNECT_NETWORK", "RESTART_CONTAINER"],
        "findings": ["Without a reconnect option in its menu the slow tier proposed 'Restart the container' (wrong); with it, it proposed the reconnect. The right proposal depended on the menu.", "Reconnecting without the compose DNS aliases left other services unable to find the database; verification now requires the alias."],
        "steps": ["Probe the connection from the host and pg_isready inside.", "Escalate (floor) and diagnose.", "Reconnect to the compose network with aliases; verify 3 fast probes plus the DNS alias."],
    },
    "dependency-down": {
        "title": "Dependency down",
        "description": "A service the web app depends on (the database container) is not running; the app reports unhealthy but is only the symptom.",
        "services": ["postgres", "web-app"],
        "signals": ["`container_state` is not 'running'", "`app_health_status` is not 200"],
        "floor": "app /health non-200 OR monitored container not running forces escalate=true and severity >= 0.8",
        "correct_fix": "RESTART_DEPENDENCY (start the container that is actually not running)",
        "actions": ["RESTART_DEPENDENCY", "RESTART_CONTAINER"],
        "findings": ["With facts about both containers the slow tier located the real cause (database exited, app only a symptom) in all runs so far.", "The executor starts only a container whose real state is not running; the target comes from code, not the model's words."],
        "steps": ["Check the app's /health and each container's state.", "Escalate (floor) and diagnose.", "Start the stopped dependency; verify DB healthy, SELECT 1, and app /health 200."],
    },
    "config-regression": {
        "title": "Config regression after deploy",
        "description": "A bad config deploy broke the web app while the database stayed healthy.",
        "services": ["web-app", "postgres"],
        "signals": ["`app_health_status` is not 200 while the database is reachable", "`last_deploy` shows a recent deploy with status 'bad' and the changed settings"],
        "floor": "the app-unhealthy floor fires (app /health non-200)",
        "correct_fix": "ROLLBACK_CONFIG (re-deploy the last known-good config)",
        "actions": ["ROLLBACK_CONFIG"],
        "findings": ["The slow tier diagnosed the changed port correctly, but without a rollback option on its menu proposed a network reconnect; with it, it proposed the rollback.", "A deploy counts as good only after 3 consecutive healthy probes; one early healthy reading was a false 'good'."],
        "steps": ["Read the app's health and the newest deploy with its config diff.", "Escalate (floor) and diagnose.", "Roll back to the newest good deploy; verify app /health 200 AND live config equals it."],
    },
}

SERVICES = {
    "postgres": ("Postgres database", "The target database container (opsmind-postgres): 256 MB memory, 0.5 CPU, 96 MB data volume."),
    "web-app": ("Web app", "A small Flask service (opsmind-app) whose /health does a real database round trip; its config is deployed from a file."),
}

# Detection order when one run matches several fault types (documented, deterministic).
PRIORITY = ["dependency-down", "oom", "disk-full", "cpu-spike", "network-partition", "config-regression"]


def _iso(ts):
    """ISO 8601 with a 'Z' suffix (spec section 5 wants an explicit UTC offset)."""
    return str(ts).replace("+00:00", "Z")


def _now():
    return _iso(datetime.now(timezone.utc).isoformat(timespec="seconds"))


def matched_faults(signal):
    """Fault types whose evidence is present in the signal, in PRIORITY order. Code only."""
    s = signal
    app = s.get("app_health_status")
    app_bad = isinstance(app, int) and not isinstance(app, bool) and app != 200
    hits = {
        "dependency-down": s.get("container_state") not in (None, "running"),
        "oom": (s.get("oom_kill_delta") or 0) > 0,
        "disk-full": s.get("write_failed") is True or (s.get("data_disk_percent_used") or 0) >= 0.95,
        "cpu-spike": (s.get("cpu_share_of_limit") or 0) >= 0.8 and (s.get("cpu_throttled_periods") or 0) > 0,
        "network-partition": s.get("db_connect_ok") is False and s.get("pg_ready_inside") is True,
        "config-regression": app_bad and (s.get("last_deploy") or {}).get("status_when_deployed") == "bad"
                             and s.get("container_state") == "running",
    }
    return [f for f in PRIORITY if hits[f]]


# ---------- reading and writing concept files ----------

def split_frontmatter(text):
    """(frontmatter dict, body) of a concept document. Raises ValueError if unparseable."""
    m = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    if not m:
        raise ValueError("no YAML frontmatter block")
    fm = yaml.safe_load(m.group(1))
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping")
    return fm, m.group(2)


def read_concept(path):
    return split_frontmatter(Path(path).read_text(encoding="utf-8"))


def write_concept(path, fm, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()
    path.write_text(f"---\n{head}\n---\n\n{body.strip()}\n", encoding="utf-8")


def _policy_line(action):
    from tools import executor
    rule = executor.load_policy().get(action)
    if rule is None:
        return f"`{action}`: not in policy.yaml (blocked)"
    mode = f"auto-approve at confidence >= {rule['min_confidence']}" if rule.get("auto_approve") else "human approval required"
    return f"`{action}`: {mode}"


def _short(run_id):
    return "run-" + run_id[:8]


def _fault_body(slug, outcomes):
    f = FAULTS[slug]
    out = [f"{f['description']}", "", "# Signals"]
    out += [f"* {x}" for x in f["signals"]]
    out += ["", "# Code floor (cannot be overridden by the model)", f["floor"] + ".",
            "", "# Correct remediation (ground truth for evaluation)", f["correct_fix"] + ".",
            "", "# Policy (from policy.yaml)"]
    out += [f"* {_policy_line(a)}" for a in f["actions"]]
    out += ["", "# Findings"] + [f"* {x}" for x in f["findings"]]
    out += ["", "# Services", ""]
    out += [f"* [{SERVICES[s][0]}](/services/{s}.md)" for s in f["services"]]
    out += ["", "See the [runbook](/runbooks/" + slug + ".md).", "", "# Verified outcomes", ""]
    if not outcomes:
        out.append("No verified automated fix has been recorded for this fault yet.")
    else:
        out += ["| When (UTC) | Run | Fast tier | Slow tier proposed | Action | Result |",
                "|---|---|---|---|---|---|"]
        for o in outcomes:
            fast = "model said no; code floor overrode" if o["overridden"] else "model escalated"
            out.append(f"| {o['at']} | {o['run_id'][:8]}[^{_short(o['run_id'])}] | {fast} | "
                       f"{o['proposed_fix']} | {o['action']} | verified: {o['verification_reason']} |")
        out.append("")
        out += [f"[^{_short(o['run_id'])}]: Run {o['run_id']} in trajectories/runs.jsonl" for o in outcomes]
    return "\n".join(out)


def _fault_front(slug, outcomes):
    f = FAULTS[slug]
    fm = {"type": "Fault Type", "title": f["title"], "description": f["description"],
          "tags": ["fault", slug], "generated": {"by": WRITER, "at": _now()}}
    if outcomes:
        fm["sources"] = [{"id": _short(o["run_id"]), "resource": f"../trajectories/runs.jsonl#{o['run_id']}",
                          "title": f"OpsMind run {o['run_id']}", "author": "opsmind-supervisor/0.1",
                          "last_modified": o["at"]} for o in outcomes]
        fm["verified"] = [{"by": VERIFIER, "at": o["at"]} for o in outcomes]
    fm["outcomes"] = outcomes   # producer-defined key: the data the table above is rendered from
    return fm


def write_fault_node(slug, outcomes, bundle=BUNDLE):
    write_concept(bundle / "faults" / f"{slug}.md", _fault_front(slug, outcomes),
                  _fault_body(slug, outcomes))


def write_static_nodes(bundle=BUNDLE):
    """Services and runbooks (derived from FAULTS/SERVICES; no outcomes data)."""
    for slug, (title, desc) in SERVICES.items():
        faults = [f for f, d in FAULTS.items() if slug in d["services"]]
        body = desc + "\n\n# Faults seen on this service\n\n" + "\n".join(
            f"* [{FAULTS[f]['title']}](/faults/{f}.md)" for f in faults)
        write_concept(bundle / "services" / f"{slug}.md",
                      {"type": "Service", "title": title, "description": desc, "tags": ["service"],
                       "generated": {"by": WRITER, "at": _now()}}, body)
    for slug, f in FAULTS.items():
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(f["steps"], 1))
        body = (f"# Trigger\n\n{f['description']} See [{f['title']}](/faults/{slug}.md).\n\n# Steps\n\n{steps}\n\n"
                f"# Safety\n\nThe model never decides alone: escalation has a code floor, the action is chosen "
                f"by rule-based mapping, policy.yaml gates execution, and success is decided by a "
                f"deterministic check, never by the model.")
        write_concept(bundle / "runbooks" / f"{slug}.md",
                      {"type": "Playbook", "title": f"Runbook: {f['title']}", "description":
                       f"How OpsMind handles: {f['title'].lower()}.", "tags": ["runbook", slug],
                       "generated": {"by": WRITER, "at": _now()}}, body)


def load_outcomes(slug, bundle=BUNDLE):
    path = bundle / "faults" / f"{slug}.md"
    if not path.exists():
        return []
    return read_concept(path)[0].get("outcomes") or []


def write_index(bundle=BUNDLE):
    """Regenerate index.md (the only index with frontmatter: okf_version)."""
    sections = {"Services": "services", "Fault types": "faults", "Runbooks": "runbooks"}
    lines = ["---", f'okf_version: "{OKF_VERSION}"', "---", ""]
    for heading, sub in sections.items():
        lines += [f"# {heading}", ""]
        for p in sorted((bundle / sub).glob("*.md")):
            fm, _ = read_concept(p)
            lines.append(f"* [{fm.get('title', p.stem)}]({sub}/{p.name}) - {fm.get('description', '')}")
        lines.append("")
    (bundle / "index.md").write_text("\n".join(lines), encoding="utf-8")


def append_log(entry, bundle=BUNDLE, today=None):
    """Newest-first, date-grouped (spec section 9). Entry is prose with a bold leading word;
    it is stored as a '* ' bullet, and every existing bullet is preserved."""
    entry = entry if entry.startswith("* ") else "* " + entry
    path = bundle / "log.md"
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    head, groups = "# Knowledge Update Log", []
    if path.exists():
        parts = re.split(r"(?m)^## ", path.read_text(encoding="utf-8"))
        for g in parts[1:]:
            date, _, rest = g.partition("\n")
            groups.append((date.strip(), [l for l in rest.splitlines() if l.startswith("* ")]))
    if groups and groups[0][0] == today:
        groups[0][1].insert(0, entry)
    else:
        groups.insert(0, (today, [entry]))
    text = head + "\n\n" + "\n\n".join(f"## {d}\n" + "\n".join(items) for d, items in groups) + "\n"
    path.write_text(text, encoding="utf-8")


def seed(bundle=BUNDLE):
    """Create/refresh every node. Existing outcomes are kept (the table is data-driven)."""
    for slug in FAULTS:
        write_fault_node(slug, load_outcomes(slug, bundle), bundle)
    write_static_nodes(bundle)
    write_index(bundle)
    if not (bundle / "log.md").exists():
        append_log("**Initialization**: Created the OpsMind knowledge bundle (services, fault types, runbooks).", bundle)


# ---------- recording a verified incident ----------

def refusal_reason(record):
    """Why this run must NOT be written to the knowledge base, or None if it may be."""
    if record.get("HARNESS_TEST"):
        return "harness/forced test run (HARNESS_TEST), not a real incident"
    ex, ver = record.get("execution") or {}, record.get("verification") or {}
    if ex.get("status") != "executed" or ex.get("dry_run") is not False:
        return "the action was not really executed (dry-run, blocked or not run)"
    if ver.get("verified") is not True:
        return "the deterministic verification did not pass"
    if not matched_faults(record.get("signal") or {}):
        return "the signal does not match any known fault type"
    return None


def record_incident(record, bundle=BUNDLE):
    """Add one verified real run to its fault node. Returns (status, detail):
    'recorded', 'already_recorded' or 'refused'."""
    why = refusal_reason(record)
    if why:
        return "refused", why
    matches = matched_faults(record["signal"])
    slug = matches[0]
    outcomes = load_outcomes(slug, bundle)
    if any(o["run_id"] == record["run_id"] for o in outcomes):
        return "already_recorded", slug
    outcomes.append({
        "run_id": record["run_id"], "at": _iso(record["timestamp"]),
        "overridden": bool(record.get("overridden")),
        "proposed_fix": (record.get("slow_tier") or {}).get("diagnosis", {}).get("proposed_fix", ""),
        "action": record["mapped_action"],
        "verification_reason": record["verification"]["reason"],
        "also_matched": matches[1:],
    })
    outcomes.sort(key=lambda o: o["at"])
    write_fault_node(slug, outcomes, bundle)
    write_static_nodes(bundle)
    write_index(bundle)
    append_log(f"**Update**: Recorded verified run {record['run_id'][:8]} in [{FAULTS[slug]['title']}]"
               f"(/faults/{slug}.md) ({record['mapped_action']}).", bundle)
    return "recorded", slug


def backfill(runs_file=RUNS_FILE, bundle=BUNDLE):
    seed(bundle)
    results = []
    for line in Path(runs_file).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if (rec.get("verification") or {}).get("verified") is True:
                results.append((rec["run_id"][:8], *record_incident(rec, bundle)))
    return results


# ---------- knowledge as slow-tier context (read side) ----------

def context_for(signal, bundle=BUNDLE, exclude_run_id=None, fault=None):
    """What the slow tier may read about this kind of incident: the fault's title, description
    and signals, plus PAST VERIFIED INCIDENTS (what was proposed, that it ran, and the code's
    verification result), in plain language.

    Deliberately NOT included: the authored 'correct remediation', the findings, the floor, the
    policy, and any action enum names. Those would hand the model an answer key. Experience only.
    `exclude_run_id` removes that run's own outcome (leave-one-out, for offline replays).
    `fault` forces a specific fault node (used only by the 'misleading knowledge' condition).
    Returns None if the signal matches no fault type."""
    slug = fault or next(iter(matched_faults(signal)), None)
    if slug is None or slug not in FAULTS:
        return None
    f = FAULTS[slug]
    past = [{"when": o["at"], "what_was_proposed": o["proposed_fix"],
             "was_executed": True, "deterministic_check_result": f"passed: {o['verification_reason']}"}
            for o in load_outcomes(slug, bundle) if o["run_id"] != exclude_run_id]
    return {"incident_type": f["title"], "description": f["description"], "signals": f["signals"],
            "previous_verified_incidents": past}


# ---------- conformance check (OKF v0.2 section 11, plus our own provenance checks) ----------

def validate(bundle=BUNDLE):
    """Returns a list of problem strings (empty = conformant)."""
    problems = []
    for p in sorted(bundle.rglob("*.md")):
        rel = p.relative_to(bundle).as_posix()
        text = p.read_text(encoding="utf-8")
        if p.name == "index.md":
            body = text
            m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
            if m:
                if p.parent != bundle:
                    problems.append(f"{rel}: only the bundle-root index.md may carry frontmatter")
                elif set(yaml.safe_load(m.group(1)) or {}) - {"okf_version"}:
                    problems.append(f"{rel}: index.md frontmatter may only hold okf_version")
                body = text[m.end():]
            if not re.search(r"(?m)^# \S", body):
                problems.append(f"{rel}: index.md has no '# Section' heading")
            continue
        if p.name == "log.md":
            dates = re.findall(r"(?m)^## (.*)$", text)
            if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.strip()) for d in dates):
                problems.append(f"{rel}: log.md date headings must be YYYY-MM-DD")
            if dates != sorted(dates, reverse=True):
                problems.append(f"{rel}: log.md must be newest first")
            continue
        try:
            fm, body = split_frontmatter(text)
        except (ValueError, yaml.YAMLError) as e:
            problems.append(f"{rel}: {e}")
            continue
        if not str(fm.get("type") or "").strip():
            problems.append(f"{rel}: missing non-empty `type`")
        ids = {s.get("id") for s in fm.get("sources") or [] if isinstance(s, dict)}
        for s in fm.get("sources") or []:
            if not (isinstance(s, dict) and s.get("resource")):
                problems.append(f"{rel}: a `sources` entry has no `resource`")
        for label in re.findall(r"\[\^([^\]]+)\]:", body):
            if label not in ids:
                problems.append(f"{rel}: footnote [^{label}] has no matching sources[].id")
        verified = fm.get("verified") or []
        for v in [verified] if isinstance(verified, dict) else verified:
            if not (isinstance(v, dict) and v.get("by") and v.get("at")):
                problems.append(f"{rel}: a `verified` entry needs `by` and `at`")
        for link in re.findall(r"\]\((/[^)#\s]+\.md)\)", body):
            if not (bundle / link.lstrip("/")).exists():
                problems.append(f"{rel}: broken link {link} (tolerated by the spec, but we keep none)")
    return problems


def trust_tier(fm):
    """Spec section 5.3, derived only from `verified`."""
    v = fm.get("verified")
    v = [v] if isinstance(v, dict) else (v or [])
    if not v:
        return "unverified"
    return "human-reviewed" if any(str(e.get("by", "")).startswith("human:") for e in v) else "machine-confirmed"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        seed()
        print("seeded", BUNDLE)
    elif cmd == "backfill":
        for r in backfill():
            print(r)
    elif cmd == "validate":
        probs = validate()
        print("conformant" if not probs else "\n".join(probs))
        sys.exit(1 if probs else 0)
    else:
        sys.exit(__doc__)

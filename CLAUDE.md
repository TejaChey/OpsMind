# OpsMind — Agentic Decision & Operations Engine

## One-line pitch
An agentic operations engine that detects, triages, decides, and remediates
software incidents autonomously — backed by a persistent, evidence-linked
knowledge base. First implementation: Autonomous Software Incident Response.

## Core principle (non-negotiable, applies to every agent)
The LLM never invents a fact and never silently decides something important
on its own judgment alone:
- Classification/severity/escalation decisions go through the FAST tier,
  schema-validated in code — if its JSON output doesn't match the schema,
  reject and retry, never accept malformed output.
- Root-cause reasoning and remediation plans go through the SLOW tier.
- Whether a fix "worked" is decided by a deterministic code-based health
  check (HTTP status, latency threshold) — NEVER by asking the LLM if it
  thinks the fix worked.

## Architecture

```
Incident signal (log, metric, health check)
                │
                ▼
          Supervisor  (worth triaging at all?)
                │
                ▼
      Fast Decision Layer
   small local model, JSON-mode, strict schema
   → classify signal / score severity / escalate? (0-1 confidence)
                │
      ┌─────────┼─────────┐
   low conf   medium     high confidence
      │          │              │
   log &      flag for     Slow Reasoning Layer
   ignore   human review   (root cause + remediation plan)
                                  │
                                  ▼
                         Tool Execution
              (restart / rollback / scale / throttle)
                                  │
                                  ▼
                   Deterministic Verification
               (code health check, never LLM-judged)
                                  │
                                  ▼
             OKF Knowledge Update + Provenance
     (patches the relevant node, cites the incident trajectory)
```

## Tech stack (all free, fully local)
- Fast decision layer: small local model (Llama 3.2 1B / Qwen2.5 0.5B) via Ollama, JSON-mode
- Slow reasoning layer: larger local model (Llama 3.1 8B / Qwen2.5 7B) via Ollama
- Knowledge/provenance format: Google OKF (markdown + YAML frontmatter)
- Tool execution: Docker SDK / subprocess
- Fault injection harness: stress-ng, tc netem, fallocate
- Target service (MVP): local Postgres or Flask container

## Repo structure (target — build incrementally, don't scaffold all at once)
```
OpsMind/
├── services/           # the target service(s) under test (MVP: one container)
├── faults/              # fault-injection scripts, one per fault type
├── agents/
│   ├── supervisor.py
│   ├── fast_tier.py     # schema-validated classification/scoring
│   └── slow_tier.py     # root cause + remediation planning
├── tools/               # restart/rollback/scale executors
├── verify/              # deterministic health-check logic
├── knowledge/           # OKF bundle — markdown + YAML frontmatter nodes
├── policy.yaml          # auto-fix vs human-review-required thresholds
└── trajectories/        # replayable JSON logs of every incident run
```

## Commands
Run from the repo root (`OpsMind/`):
```
pip install -r requirements.txt
docker compose -f services/docker-compose.yml up -d --build
python faults/oom.py
python faults/cpu_spike.py   # --duration (20), --baseline (5); samples cgroup cpu.stat
python faults/disk_full.py   # --headroom-mb (2), --rows (40); fills the capped data volume, expects real ENOSPC
```
`faults/oom.py` flags: `--container`, `--host`, `--port`, `--user`, `--password`,
`--dbname`, `--timeout` (default 60), `--settle` (default 15).
`python faults/oom.py --reset` first runs `compose down -v && up -d --build` (wipes the
DB volume) so `OOMKilled` starts False; it is opt-in because it destroys data. Without it,
`OOMKilled` stays True across runs (sticky until the container is recreated), so judge
the fault by the cgroup `oom_kill` delta, not that flag.
DB credentials and host port live only in `services/.env`; compose and `oom.py` both
read it (flags/env vars override). Host port is 5433 because a native Windows Postgres
owns 5432. Postgres's data dir is a 96 MB tmpfs-backed volume (`pgdata` in compose) so the disk fault is real: it survives `docker restart` but is wiped when the container is recreated (`compose up -d` after a compose change) or on `down -v`. Credentials only apply on first volume init: after changing them, run
`docker compose -f services/docker-compose.yml down -v`.

## Evaluation fault set (MVP ground truth)
OOM kill · CPU spike · disk full · network partition · dependency down ·
config regression after deploy — each has a known correct remediation, used
for precision/recall measurement, not just "it looks like it worked."

## Roadmap
- **MVP** — single service, all 6 fault types, full detect→decide→act→verify→log
  loop, fully local and reproducible. Build and test one fault type fully
  before adding the next.
- **Phase 1** — multi-service Docker Compose environment with cascading failures
- **Phase 2** — apply OpsMind to a second domain (e.g. CI pipeline failure triage)

## Current status
OOM fault complete end-to-end (detect→decide→diagnose→map→gate→act→verify), verified on real and harness runs. CPU spike fault complete end-to-end. Fast tier floor, mapper, policy, executor, and CPU-aware verification all confirmed on real runs. Slow tier reliably proposes "increase CPU limit" rather than query termination even with full context and an explicit action menu — correctly routed to UNKNOWN_NEEDS_HUMAN rather than forced. TERMINATE_QUERY path fully verified via harness. Finding, not a gap. Disk-full fault injector (`faults/disk_full.py`) proven real on a 96 MB capped data volume (real "No space left on device" from Postgres, volume 97.9% full) but not yet wired to any agent. Fast tier now also classifies disk signals (data_disk_percent_used from df + write_failed from a real 64 KB INSERT probe; write_failed True OR usage ≥0.95 → forced escalate, severity ≥0.8), tested on real disk-full states and on OOM/CPU-only records (no disk keys); slow tier, mapper, policy and executor don't know about disk yet. Slow tier now sees the disk facts and, on the real disk-full signal (usage 1.0, real ENOSPC), diagnoses "ran out of disk space" 3/3 and proposes "Increase the size of the data volume or remove unnecessary data" (→ UNKNOWN_NEEDS_HUMAN; menu unchanged, no disk action exists yet). Caveat: at exactly 0 bytes free Postgres can PANIC (WAL writer can't create pg_wal/xlogtemp), crash-loop and the container restart wipes the tmpfs data volume; `SELECT pg_switch_wal(); CHECKPOINT;` before the fill makes the 100%-full state survivable. Next: decide the disk action set from this output (e.g. free space / delete junk), then mapper, policy, executor, verification.

## Working conventions
- Build one fault type end-to-end before starting the next — never scaffold
  all 6 at once.
- Commit after each working fault type.
- Keep arithmetic, date logic, and pass/fail verdicts in code, never in a
  model's output.
- Update "Current status" at the end of every session (one line is enough) so
  the next session picks up where the last one left off.
## Mandatory rule: plain-language explanations
After every step (file written, command run, test added), include a short
plain-English explanation of what just happened and why — no jargon, as if
explaining to someone who only knows basic Python. 2-4 sentences max.
This applies to every future session, not just when asked.
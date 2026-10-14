Read OPSMIND_BRIEF.md at the start of every session; it holds the full plan and rules.

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
python faults/disk_full.py   # --headroom-mb (2), --rows (40); fills the capped data volume, expects real ENOSPC; --hold keeps it full, --clear cleans up
python -m unittest discover tests   # floors, mapper, policy, verification (no Docker needed)
```
`faults/oom.py` flags: `--container`, `--host`, `--port`, `--user`, `--password`,
`--dbname`, `--timeout` (default 60), `--settle` (default 15).
`python faults/oom.py --reset` first runs `compose down -v && up -d --build` (wipes the
DB volume) so `OOMKilled` starts False; it is opt-in because it destroys data. Without it,
`OOMKilled` stays True across runs (sticky until the container is recreated), so judge
the fault by the cgroup `oom_kill` delta, not that flag.
DB credentials and host port live only in `services/.env`; compose and `oom.py` both
read it (flags/env vars override). Host port is 5433 (kept from the Windows days, when a native Postgres owned 5432;
harmless on macOS). Postgres's data dir is a 96 MB tmpfs-backed volume (`pgdata` in compose) so the disk fault is real: it survives `docker restart` but is wiped when the container is recreated (`compose up -d` after a compose change) or on `down -v`. Credentials only apply on first volume init: after changing them, run
`docker compose -f services/docker-compose.yml down -v`.

## macOS setup
Verified on Apple Silicon (arm64), Docker Desktop 29.x, Python 3.11, Ollama (2026-10-14):
```
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ollama pull qwen2.5:3b && ollama pull qwen2.5:7b   # Ollama app must be running (port 11434)
docker compose -f services/docker-compose.yml up -d --build
```
- The code needed no changes for macOS: paths use `pathlib`, `docker` is called with
  argument lists (no shell), files are UTF-8, no CRLF files. Run scripts from the repo root.
- Ollama's server can take a few seconds to accept connections after launch; if
  `curl localhost:11434/api/tags` is refused, wait and retry.
- Docker Desktop runs containers in a Linux VM, so cgroup counters (`oom_kill`,
  `nr_throttled`) are real. Confirmed: `oom.py` gives oom_kill delta 1, `cpu_spike.py`
  gives 99.9% of limit with 185 throttled periods.

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
OOM fault complete end-to-end (detect→decide→diagnose→map→gate→act→verify), verified on real and harness runs. CPU spike fault complete end-to-end. Fast tier floor, mapper, policy, executor, and CPU-aware verification all confirmed on real runs. Slow tier reliably proposes "increase CPU limit" rather than query termination even with full context and an explicit action menu — correctly routed to UNKNOWN_NEEDS_HUMAN rather than forced. TERMINATE_QUERY path fully verified via harness. Finding, not a gap. Disk-full fault injector (`faults/disk_full.py`) proven real on a 96 MB capped data volume (real "No space left on device" from Postgres, volume 97.9% full). Fast tier now also classifies disk signals (data_disk_percent_used from df + write_failed from a real 64 KB INSERT probe; write_failed True OR usage ≥0.95 → forced escalate, severity ≥0.8), tested on real disk-full states and on OOM/CPU-only records (no disk keys). Slow tier now sees the disk facts and, on the real disk-full signal (usage 1.0, real ENOSPC), diagnoses "ran out of disk space" 3/3 and proposes "Increase the size of the data volume or remove unnecessary data" (menu unchanged; at that point no disk action existed). Caveat: at exactly 0 bytes free Postgres can PANIC (WAL writer can't create pg_wal/xlogtemp), crash-loop and the container restart wipes the tmpfs data volume; `SELECT pg_switch_wal(); CHECKPOINT;` before the fill makes the 100%-full state survivable. 2026-10-14: moved to macOS (Apple Silicon); no code changes needed, oom.py (oom_kill delta 1) and cpu_spike.py (99.9% of limit, 185 throttled periods) re-confirmed real. DISK FAULT COMPLETE end-to-end (2026-10-14): mapper has INCREASE_DISK + FREE_DISK_SPACE, both auto_approve:false in policy.yaml (deleting is human-only); the model's compound "increase volume OR remove data" correctly maps to UNKNOWN_NEEDS_HUMAN; no automatable disk action exists (restart doesn't free the volume), accepted per brief s7. Disk-aware verification (write probe ok AND usage < 0.90, fail-closed) proven on a real held fault: health-only check false-passed (verified=True) while the disk check failed; after removing the filler it passed. `faults/disk_full.py --hold` keeps the fault live, `--clear` cleans up (also drops opsmind_write_probe). Full supervisor run on the real fault logged to runs.jsonl (model escalated by itself that time, floor not needed); 2 idle controls were correct (sev 0.2, no escalate), 0.8-idle quirk not seen. tests/ now exists (`python -m unittest discover tests`, 31 tests: OOM/CPU/disk floors, mapper, policy, disk verification). FAULT 4 (network partition): `faults/network_partition.py` (real `docker network disconnect`; --hold/--clear) proven real: host connect times out while container is running and pg_isready INSIDE says ready (Docker's own health stays 'healthy'). Fast tier: new signal fields pg_ready_inside + db_connect_seconds; floor = db_connect_ok False AND pg_ready_inside True (sev>=0.8); on the real partition the model said escalate=false and the floor overrode it. Slow tier: WITHOUT a reconnect option in its menu it proposed 'Restart the container' 3/3 (wrong fix, policy would auto-approve it); WITH 'reconnect the container to its Docker network' added to the plain-language menu it proposed that 3/3 -> RECONNECT_NETWORK (finding: correct proposal is menu-dependent). RECONNECT_NETWORK: mapper + policy (auto_approve, min_conf 0.7) + executor (network from compose label; no_target if already connected), dry-run proven; network verification (3 host probes all ok and < 1.0s) fails while partitioned and passes after reconnect. Known quirk: fast model's idle severity is 0.8-0.85 with escalate=false (reproduces on unmodified HEAD; harmless since escalate=false decides). 42 tests. FAULT 4 COMPLETE: first --live RECONNECT_NETWORK approved by user and run on the real partition (run 62e6d114): floor override -> slow tier -> executed (networks [] -> ['services_default']) -> verified (3 probes <1.0s), checked in runs.jsonl. Next: Fault 5, dependency down (brief s8.3; start of Phase 1, second service).

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
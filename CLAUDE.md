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
python eval/run_eval.py && python eval/run_baselines.py && python eval/summarize_eval.py --compare && python eval/make_charts.py   # full evaluation (~35 min, needs Ollama+Docker, injects real faults)
python eval/slow_tier_regression.py   # standing slow-tier regression gate (needs Ollama); run after ANY slow-tier/prompt/knowledge change
python tools/knowledge.py validate   # OKF v0.2 conformance of knowledge/ (also: seed, backfill)
streamlit run dashboard/app.py   # read-only dashboard: runs + trajectory viewer, eval tables/figures, knowledge bundle
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
OOM fault complete end-to-end (detect→decide→diagnose→map→gate→act→verify), verified on real and harness runs. CPU spike fault complete end-to-end. Fast tier floor, mapper, policy, executor, and CPU-aware verification all confirmed on real runs. Slow tier reliably proposes "increase CPU limit" rather than query termination even with full context and an explicit action menu — correctly routed to UNKNOWN_NEEDS_HUMAN rather than forced. TERMINATE_QUERY path fully verified via harness. Finding, not a gap. Disk-full fault injector (`faults/disk_full.py`) proven real on a 96 MB capped data volume (real "No space left on device" from Postgres, volume 97.9% full). Fast tier now also classifies disk signals (data_disk_percent_used from df + write_failed from a real 64 KB INSERT probe; write_failed True OR usage ≥0.95 → forced escalate, severity ≥0.8), tested on real disk-full states and on OOM/CPU-only records (no disk keys). Slow tier now sees the disk facts and, on the real disk-full signal (usage 1.0, real ENOSPC), diagnoses "ran out of disk space" 3/3 and proposes "Increase the size of the data volume or remove unnecessary data" (menu unchanged; at that point no disk action existed). Caveat: at exactly 0 bytes free Postgres can PANIC (WAL writer can't create pg_wal/xlogtemp), crash-loop and the container restart wipes the tmpfs data volume; `SELECT pg_switch_wal(); CHECKPOINT;` before the fill makes the 100%-full state survivable. 2026-10-14: moved to macOS (Apple Silicon); no code changes needed, oom.py (oom_kill delta 1) and cpu_spike.py (99.9% of limit, 185 throttled periods) re-confirmed real. DISK FAULT COMPLETE end-to-end (2026-10-14): mapper has INCREASE_DISK + FREE_DISK_SPACE, both auto_approve:false in policy.yaml (deleting is human-only); the model's compound "increase volume OR remove data" correctly maps to UNKNOWN_NEEDS_HUMAN; no automatable disk action exists (restart doesn't free the volume), accepted per brief s7. Disk-aware verification (write probe ok AND usage < 0.90, fail-closed) proven on a real held fault: health-only check false-passed (verified=True) while the disk check failed; after removing the filler it passed. `faults/disk_full.py --hold` keeps the fault live, `--clear` cleans up (also drops opsmind_write_probe). Full supervisor run on the real fault logged to runs.jsonl (model escalated by itself that time, floor not needed); 2 idle controls were correct (sev 0.2, no escalate), 0.8-idle quirk not seen. tests/ now exists (`python -m unittest discover tests`, 31 tests: OOM/CPU/disk floors, mapper, policy, disk verification). FAULT 4 (network partition): `faults/network_partition.py` (real `docker network disconnect`; --hold/--clear) proven real: host connect times out while container is running and pg_isready INSIDE says ready (Docker's own health stays 'healthy'). Fast tier: new signal fields pg_ready_inside + db_connect_seconds; floor = db_connect_ok False AND pg_ready_inside True (sev>=0.8); on the real partition the model said escalate=false and the floor overrode it. Slow tier: WITHOUT a reconnect option in its menu it proposed 'Restart the container' 3/3 (wrong fix, policy would auto-approve it); WITH 'reconnect the container to its Docker network' added to the plain-language menu it proposed that 3/3 -> RECONNECT_NETWORK (finding: correct proposal is menu-dependent). RECONNECT_NETWORK: mapper + policy (auto_approve, min_conf 0.7) + executor (network from compose label; no_target if already connected), dry-run proven; network verification (3 host probes all ok and < 1.0s) fails while partitioned and passes after reconnect. Known quirk: fast model's idle severity is 0.8-0.85 with escalate=false (reproduces on unmodified HEAD; harmless since escalate=false decides). 42 tests. FAULT 4 COMPLETE: first --live RECONNECT_NETWORK approved by user and run on the real partition (run 62e6d114): floor override -> slow tier -> executed (networks [] -> ['services_default']) -> verified (3 probes <1.0s), checked in runs.jsonl. FAULT 5 (dependency down): second service `services/app` (Flask, container opsmind-app, host port 5050, /health does a real DB round trip). `faults/dependency_down.py` (real `docker stop` of Postgres; --hold/--clear) proven real: app /health 200->503 while the app container keeps running. BUG FOUND+FIXED via the new app: Fault 4's reconnect dropped compose DNS aliases (app could not resolve 'postgres') and the host-only network verification false-passed it; reconnect now restores aliases (executor + script) and network verification requires the alias. Fast tier: signal now has container_state + app_health_status/app_health_error/app_container_state, None (never 0) when unmeasurable; floors: app /health non-200 OR monitored container not running (sev>=0.8); on the real fault the model said escalate=false and the floor overrode it (5th fault type). Slow tier located the real cause 3/3 (database container exited; app only a symptom) and proposed 'Restart the database container' with no menu change. RESTART_DEPENDENCY: mapper (specific beats generic RESTART_CONTAINER), policy auto_approve 0.7, executor starts ONLY containers whose real state is not running (target chosen in code), dry-run proven; verification = DB healthy + SELECT 1 + app /health 200 with db_ok (retried), fails while down, passes after. Weakness FIXED: restart proposals that name the app as the thing to restart (e.g. 'Restart the application') now map to UNKNOWN_NEEDS_HUMAN, because restart actions always target the database container chosen in code (a 'refuse to restart a running container' guard was rejected: restarting a running container is the legitimate RESTART_CONTAINER use). 65 tests. FAULT 5 COMPLETE: first --live RESTART_DEPENDENCY approved by user and run on the real fault (run c345bb38): floor override -> slow tier -> executed (opsmind-postgres exited -> running) -> verified (DB healthy + SELECT 1 + app /health 200 db_ok, 6.1s), checked in runs.jsonl. FAULT 6 (config regression after deploy): `tools/deploy.py` keeps deploys/history.jsonl (+ live config deploys/live/app.json, both gitignored runtime state, mounted into the app container; the app re-reads it per request). Deploy status (good/bad) is decided by code: 3 consecutive healthy /health probes (BUG FOUND+FIXED: a single early healthy reading right after a deploy was a false 'good' because the app still ran the old config, which then made the rollback a no-op). `faults/config_regression.py` (--variant port|host, --hold/--clear) deploys a bad config through the real deploy tool: app /health 200->503 while Postgres is fine (direct connect ok) and history says status=bad. Signal: supervisor attaches `last_deploy` (id, age, status, config diff vs last known-good) for the slow tier; the existing app-unhealthy floor already fires (model said escalate=false, floor overrode: 6th fault type). Slow tier diagnosed the port change correctly 3/3, but WITHOUT a rollback option on its menu proposed 'Reconnect the container to its Docker network' (wrong remedy; the executor's no_target guard made it harmless); WITH 'roll back the web app's config to the last known-good deploy' on the menu it proposed that 3/3 (menu-dependence again, 3rd time). ROLLBACK_CONFIG: mapper, policy (auto_approve, min_conf 0.8), executor (target = newest good deploy chosen in code; no_target if live config already equals it; failed if app unhealthy after), dry-run proven; verification = app /health 200 db_ok AND live config == last known-good config, fails while the bad deploy is live. 94 tests. FAULT 6 COMPLETE: first --live ROLLBACK_CONFIG approved by user and run on the real bad deploy (run 0a71189a): floor override -> slow tier (diagnosed the port change) -> executed (db_port 5439 -> 5432 via a recorded rollback deploy) -> verified (app /health 200 db_ok AND live config == known-good), checked in runs.jsonl + deploys history. ALL 6 MVP FAULTS COMPLETE. OKF KNOWLEDGE LAYER BUILT (2026-10-14), from the REAL spec (read raw okf/SPEC.md v0.2, not from memory): `knowledge/` bundle (index.md with okf_version, log.md newest-first, services/, faults/ x6 type 'Fault Type', runbooks/ x6 type 'Playbook'), written ONLY by code (`tools/knowledge.py`: seed / backfill / validate), never by a model. After a VERIFIED real incident the supervisor's main() (not run_once, which the harness also calls) adds the run to its fault node: `sources` entry citing the run_id (resource ../trajectories/runs.jsonl#<run_id>), `verified` by process:opsmind-verify (trust tier machine-confirmed, never human-reviewed), outcomes table row + footnote keyed to the source id, log.md entry. Refused: HARNESS_TEST, dry-run, not-executed, unverified. Idempotent. Fault matching is deterministic code with a documented priority order. Backfilled the 3 real verified runs (Faults 4/5/6) and one new live ROLLBACK_CONFIG run updated the bundle automatically (checked in the file). BUG found by reading the file: log.md dropped old entries (bullet prefix); fixed + regression test (mutation-tested). `python tools/knowledge.py validate` -> conformant. 104 tests. OOM/CPU/disk nodes have no verified outcomes (their fixes are human-only/never executed), so their tables say so. KNOWLEDGE EXPERIMENT DONE (eval/knowledge_experiment.py, results + limits in eval/results/knowledge_experiment_NOTES.md; slow tier can optionally read past verified incidents via context_for(), OFF by default, no answer key, leave-one-out): (1) REGRESSION FOUND: with today's slow-tier prompt the replayed Fault 4/5 incidents are answered wrongly (network 0/7, dependency 0/5) though correct live; git-bisecting real prompt versions shows the Fault 6 prompt text flipped them (prompt is fragile; I had not re-run the slow tier on earlier faults when changing the prompt). (2) Showing past verified incidents restores them (7/7, 5/5), keeps config 8/8; (3) it can stand in for a missing menu item (config 1/8->8/8); (4) no help without verified experience (OOM ablated 0/6); (5) HARM: with the wrong fault's file the model copied the wrong remedy 20/20, so it follows precedent, safe only as far as the code-side matching is. REGRESSION FIXED (user delegated the choice; option C): (a) STANDING CHECK `python eval/slow_tier_regression.py` (replays every real escalated incident through the slow tier as production does, scores in code vs ground truth, exits 1 below eval/slow_tier_thresholds.json, writes eval/results/slow_tier_regression_latest.json with git commit + prompt hashes): RUN IT AFTER ANY CHANGE to slow_tier.py / prompt / menu / facts / knowledge / model. Proven to bite: knowledge off -> network 3/7 -> exit 1; production config -> all faults 100% (n=27 total; thresholds are a ratchet at current performance, lowering needs a justification). (b) prompt: the deploy paragraph and the rollback menu item are now included only when the input has last_deploy (slow_tier.build_system_prompt; identical to the old prompt when deploy facts are present, tested) - offer only what exists for this incident, not steering. Alone this gives dependency 5/5, config 8/8, network 3/7 (was 0/7). (c) knowledge now ON by default in supervisor.main (`--no-knowledge` to disable); context_for returns None when the signal matches zero OR MORE THAN ONE fault (code-side guard against the wrong-precedent harm); the exact context shown is logged in each trajectory line as `knowledge_context`. Verified on a real network partition (dry-run): reconnect proposed, 1 past incident shown, logged. disk-full ground truth now accepts UNKNOWN_NEEDS_HUMAN (all disk outcomes are human-only by policy). GENERALISATION TEST DONE (real `config_regression.py --variant host`, a variant the knowledge never saw; notes in eval/results/knowledge_experiment_NOTES.md): knowledge ON -> ROLLBACK_CONFIG 3/3 (correct); knowledge OFF -> RECONNECT_NETWORK 3/3 (wrong, accurate diagnosis but wrong remedy, harmless via no_target but unresolved); live rollback executed + verified + auto-recorded in the knowledge bundle; standing gate still PASS on 35 real incidents. Limits: one near variant, n=3, deterministic. Honest caveats: the model's behaviour is still prompt-fragile; the replay set is the same incidents the knowledge was learned from (leave-one-out, same fault variants), so generalisation to NEW variants is untested; precedent-following means knowledge is only as safe as the fault matching. EVALUATION SUITE DONE (brief s8.8), all in eval/ (notes with limits: eval/results/eval_full_NOTES.md, eval_baselines_NOTES.md): stage 1 `run_eval.py` = full config LIVE on real injected faults (10 trials x 6 faults + 10 quiet controls, 0 harness errors), scored by INDEPENDENT ground truth (`ground_truth.py`), writes only to eval/results/ (never runs.jsonl or knowledge/), live actions only for user-approved kinds (guard unit-tested); stage 2 `run_baselines.py` = baselines (single always-on 7b; fixed rules, no LLM) + ablations (no floor / knowledge / policy gate / health-only verification) replayed OFFLINE on the same 70 signals (decision quality only, not fix success); `summarize_eval.py [--compare]` -> CSVs (Wilson CIs); `make_charts.py` -> eval/results/charts/fig1-4 (png+svg). RESULTS: detection 60/60 (model alone 9/60 = floor essential), 0/10 false positives, 30/30 auto-fixes recovered per independent ground truth, mapping 50/60 (all 10 misses = CPU: slow tier proposes 'increase MEMORY limit' 10/10, human-blocked); vs single 7b 27/60 detected, fixed rules 33% correct action with 40/60 wrong actions policy would let run, full system 0. HARNESS BUGS found+fixed: disk injector destroyed its own fault (WAL PANIC -> restart wiped tmpfs; confirmed in container log; now checkpoints first + independent fault-present validity check), 'time to detect' was end-of-pipeline (now real), run_once records per-stage timings + token usage. FINDING: every production signal now carries last_deploy, so the conditional-prompt change is inert; the regression gate now also replays the 60 live eval incidents (95 total, cpu-spike threshold 0.0 = documented known finding) and passes. Honest limits: n=10/fault, one machine, knowledge learned from dev runs of the same faults, offline configs cannot show fix success, fault-specific verification's benefit not shown in live trials (all fixes were correct). DASHBOARD DONE (2026-10-15): `dashboard/app.py` (Streamlit, read-only; tabs: incident runs + per-run trajectory viewer, evaluation tables/figures, knowledge bundle) over `dashboard/data.py` (stats in code, unit-tested; headline counts cross-checked against grep of runs.jsonl: 34/38 overridden, 5/5 live fixes verified; app loaded headlessly via streamlit AppTest with no exceptions, but not yet eyeballed in a real browser). 139 tests. README DONE (2026-10-15): `README.md` (mermaid architecture diagram, setup, demo, results table + comparison, findings, limits; every number copied from eval/results/*.csv and notes; Windows path not re-run since the Mac move, stated in the README; dashboard screenshots not included, no browser available). FIXES DONE (2026-10-15): (1) `tools/jsonl_io.append_jsonl` (fcntl flock; Windows msvcrt branch untested) now used by supervisor.write_trajectory and deploy.append_history; test holds the lock from another process and checks the writer waits (mutation-tested: fails without the lock; a first 'concurrent writers' test passed even without the lock so it was replaced). (2) TERMINATE_QUERY guard: executor only targets an active query older than MIN_TARGET_SECONDS (5) owned by the service's DB user, reports what it skipped (`skipped_candidate`); real-Postgres tests (skip if DB down), both guards mutation-tested. (3) `eval/verification_wrong_fix.py` + notes: on 4 held real faults plain health check false-passed 2/4 (config, disk), fault-specific 0/4, control passes 4/4 (n=1 each). INCREASE_CPU_LIMIT ADDED (user asked; mapper rule, policy auto_approve false, no executor, menu item): replay of 95 incidents -> cpu-spike 10/10 now propose INCREASE_CPU_LIMIT (was 10/10 memory), all other faults still 100%, 0/22 stored proposals remap differently; SCORING DECIDED (user delegated, 2026-10-15): strict ground truth unchanged and primary (cpu-spike TERMINATE_QUERY, strict 0/10); a clearly-labelled SECOND number also accepts INCREASE_CPU_LIMIT via knowledge_experiment.ALTERNATIVE_ACCEPTABLE/score_mapped (10/10), gate prints both, threshold alt:cpu-spike 1.0, post-hoc so always report both, FULL EVAL RE-RUN after the menu change (2026-10-15, archive of the previous run in eval/results/archive_run2_*): recall 60/60, model alone 10/60, FP 0/10, auto-fix 30/30, 0 verifier false-pass, 0 harness errors; mapping strict 50/60 (all CPU misses), incl. alt 60/60; BASELINE SHIFT FOUND: the menu change moved other things: single_7b recall 27/60 -> 10/60 (baseline reuses the menu, report as a range), no_knowledge OOM 7/10 -> 1/10 (proposes CPU limit 9/10 without knowledge; production with knowledge unaffected, OOM 10/10) so production OOM now leans on the knowledge layer; summarize_eval has a mapping_incl_alt column; notes rewritten (eval_full_NOTES.md, eval_baselines_NOTES.md), README + VERIFY_WITH_CLAUDE.md updated; standing gate RE-RUN after the eval on a clean tree (commit 893d774, 95 incidents incl. the 60 new eval ones, 0 fallbacks): PASS, other faults 100%, cpu-spike strict 0/10 incl. alt 10/10; CPU RECOVERY RE-SAMPLE DONE: verify/health_check.py re-samples a still-high CPU reading up to CPU_RESAMPLES=2 more times, LAST sample decides, unmeasurable fails closed and is never retried, all samples in `cpu_samples`; 8 mock-based tests, mutation-tested (no-retry and use-min-sample both fail tests), real idle container verified with 1 sample; NOT yet exercised on a real post-TERMINATE_QUERY run (no automatable CPU live path approved); notes in eval/results/cpu_menu_NOTES.md. 152 tests (2026-10-21 run, 1 skipped). Next: paper draft (IEEE structure), deck; optional: Phase 1 cascading failures, Phase 2 CI-triage domain.

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
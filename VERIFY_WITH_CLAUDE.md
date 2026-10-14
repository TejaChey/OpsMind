# OpsMind: independent verification brief

**Purpose.** A previous Claude Code session (Sonnet 5.5, 2026-10-14) did a lot of work on this repo and
reported results. You are a *fresh, independent* reviewer. Your job is to check whether what was
claimed was actually done, and to look for overclaims. **Do not trust the previous session's summaries,
`CLAUDE.md`'s "Current status", or commit messages as evidence.** Trust only files you open and commands
you run. Report anything that does not reproduce, plainly, with the evidence.

Start by reading `OPSMIND_BRIEF.md` (the project's rules and plan; it is gitignored and untracked, so it
exists only on this machine), then `CLAUDE.md`, then this file. The rules in the brief section 4 are the
standard everything is judged against (never weaken a floor, forced tests only in harness logs, verify
from the file not the print, arithmetic and pass/fail in code, etc.).

## 0. What was asked of that session (in order)
1. Add a pointer line at the top of `CLAUDE.md`; do a macOS setup check (Docker, venv, Ollama models); find
   and fix only Windows-specific code; run `faults/oom.py` and `faults/cpu_spike.py` once each and confirm
   they are real on macOS; add a "macOS setup" section + update status.
2. Finish Fault 3 (disk full) per brief section 6, then continue Fault 4 (network partition), Fault 5
   (dependency down, starts Phase 1), Fault 6 (config regression after deploy).
3. Fix a mapper weakness ("Restart the application" restarting the database).
4. Build the OKF knowledge + provenance layer from the REAL spec; run the "does knowledge change the slow
   tier" experiment; fix the regression it exposed; run a generalisation test.
5. Build the evaluation suite (brief 8.8): runner, baselines, ablations, CSVs, charts.
6. The user also asked to remove the "suggest /clear after each milestone" rule from the brief.

## 1. Quick offline checks (no Docker, no Ollama; run these first)
```
cd /Users/teja/Projects/OpsMind && source .venv/bin/activate
python -m unittest discover tests            # expect: Ran 134 tests ... OK
python tools/knowledge.py validate           # expect: conformant
python eval/summarize_eval.py --compare      # recomputes the comparison table from saved rows
python eval/make_charts.py                   # regenerates eval/results/charts/*
git log --oneline | wc -l                    # expect 30; every milestone is its own commit
git status --short                           # expect clean (OPSMIND_BRIEF.md is ignored)
```
Expected `--compare` rows (config, recall, false-pos, mapping accuracy, wrong-named, wrong-that-policy-lets-run):
full 1.0/0.0/0.833/10/0 · single_7b 0.45/0.0/0.167/16/16 · fixed_rules 1.0/0.0/0.333/40/40 ·
no_floor 0.15/0.0/0.15/0/0 · no_knowledge 1.0/0.0/0.433/21/20 · no_policy_gate 1.0/0.0/0.833/10/10 ·
health_only_verification same as full (verifier false-pass 0/30).

## 2. Claims, where the evidence is, and how to check
| # | Claim | Evidence | How to verify |
|---|---|---|---|
| A1 | macOS: Docker, venv (py3.11), Ollama qwen2.5:3b/7b work | `CLAUDE.md` "macOS setup" | `docker version`, `.venv/bin/python --version`, `ollama list`; curl `localhost:11434/api/tags` |
| A2 | No Windows-specific code needed fixing (pathlib, argv-list subprocess, UTF-8, no CRLF) | grep result; only a CLAUDE.md wording change | `grep -rn "T:/\|\.exe\|powershell" --include=*.py .` (ignore `.venv`); `file` on text files for CRLF |
| A3 | `oom.py` (oom_kill delta>0) and `cpu_spike.py` (~99.9% of limit, throttled periods>0) are real on macOS | commit d58766c message + CLAUDE.md | re-run both; look for "FAULT TOOK EFFECT" |
| B | Fault 3 disk: mapper actions `INCREASE_DISK`, `FREE_DISK_SPACE` (both human-only), disk-aware verification (write probe ok AND usage<0.90, fail-closed), `--hold/--clear`, 3 supervisor runs | `agents/action_mapper.py`, `policy.yaml`, `verify/health_check.py`, `faults/disk_full.py`, `tests/test_floors.py`, `tests/test_disk_verification.py` | run tests; `python faults/disk_full.py --hold` then verify, then `--clear` |
| C | Fault 4 network: `faults/network_partition.py` (real `docker network disconnect`), floor = host cannot connect AND `pg_ready_inside`, `RECONNECT_NETWORK` live-verified once | `trajectories/runs.jsonl` run 62e6d114 (`execution.status=executed`, `dry_run=false`, networks `[]`->`['services_default']`, `verification.verified=true`) | open that JSONL line; re-run fault + `agents/supervisor.py --live` (asks nothing; only touches the test container) |
| C2 | Bug found+fixed: reconnect lost compose DNS aliases; verification now requires the alias | `tools/executor.py::reconnect_network`, `verify/health_check.py::_service_alias_missing`, tests | `docker inspect opsmind-postgres` after a partition+clear shows aliases `postgres`, `opsmind-postgres` |
| D | Fault 5 dependency-down: second service `services/app` (Flask, host port 5050), `faults/dependency_down.py`, `RESTART_DEPENDENCY` (starts only a container whose real state is not running), live-verified once | runs.jsonl run c345bb38; `tests/test_dependency.py` | `curl localhost:5050/health`; re-run fault + supervisor `--live` |
| E | Fault 6 config regression: `tools/deploy.py` (history, good/bad status decided by 3 consecutive healthy probes), `faults/config_regression.py` (`--variant port|host`), `ROLLBACK_CONFIG` live-verified | runs.jsonl run 0a71189a; `deploys/` (gitignored runtime state); `tests/test_deploy.py`, `tests/test_rollback.py` | `python tools/deploy.py` prints history; re-run fault + supervisor |
| F | Mapper: restart proposals naming the app map to `UNKNOWN_NEEDS_HUMAN` (a "refuse to restart a running container" guard was deliberately rejected as wrong) | `agents/action_mapper.py`, `tests/test_dependency.py` | `python agents/action_mapper.py` |
| G | OKF layer built from the real spec v0.2 (raw SPEC.md was downloaded and read, not paraphrased), written ONLY by code, refuses harness/dry-run/unverified runs, idempotent, trust tier machine-confirmed, hook in `supervisor.main()` NOT `run_once` (the harness calls `run_once`) | `knowledge/`, `tools/knowledge.py`, `tests/test_knowledge.py` | `validate`; open `knowledge/faults/config-regression.md`: sources cite run_ids, footnotes match ids; compare with the spec at https://raw.githubusercontent.com/GoogleCloudPlatform/knowledge-catalog/main/okf/SPEC.md |
| H1 | Experiment: showing past verified incidents changes slow-tier proposals (126 calls, all model-sourced, leave-one-out, no answer key leaked); misleading precedent was followed 20/20 | `eval/results/knowledge_experiment_raw.jsonl`, `..._summary.csv`, `..._NOTES.md`; `tests/test_knowledge_context.py` (leak guard) | `python eval/knowledge_experiment.py --summary-only`; read `tools/knowledge.py::context_for` for what the model is shown |
| H2 | Regression I introduced (Fault 6 prompt text broke Faults 4/5) was found by git-bisecting the real historical prompts, and fixed (knowledge on by default + conditional deploy prompt pieces + ambiguity guard) | `NOTES.md`; `eval/slow_tier_regression.py` | `python eval/slow_tier_regression.py` (needs Ollama, ~7 min) expect PASS, exit 0; `--knowledge off` expect network FAIL, exit 1 |
| I | Generalisation test on an unseen variant (`config_regression.py --variant host`): knowledge ON proposed rollback 3/3, OFF proposed network reconnect 3/3; live rollback verified and auto-recorded in knowledge | `knowledge_experiment_NOTES.md` (bottom); runs.jsonl run 90383827 (host-variant live rollback, `config_diff` db_host postgress->postgres, verified true); the other live rollbacks are 0a71189a and a530b6a9 (port variant) | reproduce with `--variant host --hold` and `agents/supervisor.py` with/without `--no-knowledge` |
| J1 | Eval stage 1: 70 live jobs (10 quiet controls + 10 trials x 6 faults), 0 harness errors; independent ground truth in `eval/ground_truth.py` (shares no code with `verify/`) | `eval/results/eval_runs.jsonl`, `eval_summary_full.csv`, `eval_full_NOTES.md` | `summarize_eval.py`; open any row's `record`; confirm `gt_recovered` comes from `ground_truth.recovered` |
| J2 | Eval stage 2: baselines (single always-on 7b; fixed rules) and ablations (no floor / knowledge / policy gate / health-only verification) replayed offline on the SAME 70 saved signals | `eval/results/eval_runs_offline.jsonl` (420 rows = 6 configs x 70), `eval_baselines_NOTES.md` | `--compare`; read `fixed_rules()` in `eval/run_baselines.py` and judge whether the rule set is fair |
| J3 | Four charts (png+svg), palette validated with the dataviz skill's script | `eval/results/charts/` | open them; check numbers against the CSV |

## 3. Things to actively try to break (red-team list)
1. **Safety guard.** `eval/run_eval.py::guard_executor` must force dry-run for any action outside
   `{RECONNECT_NETWORK, RESTART_DEPENDENCY, ROLLBACK_CONFIG}` and for OOM/CPU/disk. Read it and `tests/test_eval_runner.py`.
2. **Eval isolation.** Confirm eval runs never wrote to `trajectories/runs.jsonl` or `knowledge/` during the
   eval (compare `runs.jsonl` timestamps/run_ids with `eval_runs.jsonl`; `grep record_incident eval/*.py`).
3. **Floors not weakened.** Compare `agents/fast_tier.py::apply_floor` with the brief; mutate a threshold
   (e.g. `DISK_USED_THRESHOLD=0.99`) and confirm tests fail; restore. Clear `__pycache__` between mutations
   (a stale `.pyc` once masked a restore).
4. **Model-alone recall 9/60.** It is computed from each live row's `model_raw.escalate`; confirm in the JSONL.
5. **Independent ground truth really is independent.** `eval/ground_truth.py` vs `verify/health_check.py`.
6. **Knowledge leak.** `context_for` must not expose the authored `correct_fix`, findings, policy or enum
   names (`tests/test_knowledge_context.py::test_no_answer_key_in_the_context`).
7. **Knowledge records only real verified runs.** Try `record_incident` on a harness record in `trajectories/harness_runs.jsonl`: must be refused.
8. **Stats.** Spot-check Wilson intervals and the CSV arithmetic by hand for one row.
9. **Each "FAULT TOOK EFFECT" is code-decided**, not printed unconditionally (read each `faults/*.py`).
10. **Idempotence.** Run `python tools/knowledge.py backfill` twice: the second run must change nothing.

## 4. Disclosed deviations, mistakes and open caveats (check that these are true and that nothing else is hidden)
* **`OPSMIND_BRIEF.md` was never committed**: it is in `.gitignore` (line 4). An early message wrongly said it was committed. The `/clear` rule was removed from it at the user's request (on disk only).
* **Live approvals.** The user explicitly approved the FIRST live run of `RECONNECT_NETWORK`, `RESTART_DEPENDENCY`, `ROLLBACK_CONFIG` (each asked and answered). Later, additional live runs of those SAME approved kinds were made without asking each time: an executor alias test, a second and third rollback (incl. host variant), and the 30 live eval fixes. `RESTART_CONTAINER` and `TERMINATE_QUERY` were never run live in the real pipeline (harness only); `INCREASE_*`/`FREE_DISK_SPACE` have no executors.
* **`policy.yaml` entries added** (brief says ask before policy changes; the user said "go ahead / whatever you think is good"): human-only `INCREASE_DISK`, `FREE_DISK_SPACE`; auto-approve `RECONNECT_NETWORK` (0.7), `RESTART_DEPENDENCY` (0.7), `ROLLBACK_CONFIG` (0.8).
* **Mistakes found by the work itself and fixed**: reconnect lost DNS aliases; first deploy status was a false "good" (one early healthy probe); log.md dropped old entries; the Fault 6 prompt change regressed Faults 4/5 (no slow-tier regression check existed); the disk injector destroyed its own fault via a Postgres PANIC (run 1 archived as `archive_run1_*`); "time to detect" was end-of-pipeline; the regression gate replayed stale signals.
* **Still weak / not solved**: CPU spike (slow tier proposes "increase memory limit" 10/10; gate threshold 0.0 documents it); the slow tier is prompt-fragile; the conditional deploy-prompt change is inert in practice because every production signal now carries `last_deploy`.
* **Not live-demonstrated**: the benefit of fault-specific verification (all live fixes were correct, so health-only false-pass was 0/30); it was shown only on held faults earlier.
* **Evaluation limits**: n=10 per fault; one machine; temperature 0; the knowledge was learned from earlier dev runs of the same faults; baselines are one reasonable implementation each and run OFFLINE (decision quality only, no fix success).
* **NOT DONE**: dashboard, README, paper draft, slide deck, Phase 1 cascading failures (only a 2-service environment exists), Phase 2 CI-triage domain, brief backlog items (TERMINATE_QUERY targeting guard, JSONL file lock, CPU recovery-threshold tuning), and a Windows re-test after the changes (macOS only).

## 5. What a good review reports
For each row in section 2: PASS / FAIL / NOT REPRODUCIBLE with the command and output. Then: any claim in
section 4 that is false or incomplete, any overclaim in `CLAUDE.md` or `eval/results/*_NOTES.md`, any
rule in the brief section 4 that was broken, and anything that looks unsafe (live actions, policy).

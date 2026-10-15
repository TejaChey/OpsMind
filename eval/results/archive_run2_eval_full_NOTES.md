# Evaluation stage 1: full OpsMind configuration on real injected faults (2026-10-14)

Data: `eval_runs.jsonl` (70 jobs: 10 quiet controls + 10 trials x 6 faults), `eval_summary_full.csv`,
`eval_summary_full_overall.json`. Runner `eval/run_eval.py`, metrics `eval/summarize_eval.py`,
independent ground truth `eval/ground_truth.py`. 0 harness errors, 0 excluded trials.
Run 1 (archived as `archive_run1_*`) had 2 disk trials whose fault destroyed itself (Postgres WAL
PANIC -> restart wiped the tmpfs volume; confirmed in the container log) and no real timing; the
injector and harness were fixed and everything was re-run.

## Headline numbers (valid trials = 10 per fault)
* Detection recall: 60/60 (each fault 10/10; Wilson 95% CI 0.72-1.0 per fault).
* **Model alone (before the code floor) detected only 9/60**: the floor overrode the model in 51/60.
* False positives on quiet controls: 0/10 (95% CI 0-0.28); model alone also 0/10.
* Mapping accuracy (proposal maps to the ground-truth action): 50/60. All of the 10 misses are CPU.
* Auto-fix (network, dependency, config; live, only user-approved action kinds): 30/30 recovered
  per INDEPENDENT ground truth; verifier false-pass 0/30; health-only false-pass 0/30.
* Latency (medians): fast tier ~0.5 s, slow tier 2.0-3.6 s; ~700 fast / 1000-1400 slow tokens.
  Time to recover (auto-fixable): 8.7 s dependency, 11.6 s config, 16.9 s network.
  Time to detect: 0.4 s dependency (container down: no sampling window needed), 5.8 s disk,
  6.0 s config, 10.9 s network, 22 s CPU, 41 s OOM (the window is part of detection for rate faults).

## Findings, not smoothed over
1. The small fast model is useless alone for detection (15% recall); the deterministic floor is what
   makes detection work. Its 0/10 false-positive rate is on only 10 controls.
2. CPU: the slow tier diagnoses correctly ("pinned at its CPU limit") but proposed "Increase the
   container's MEMORY limit" 10/10 (the prompt's menu has a memory-limit item and no CPU-limit item).
   Wrong named action, but policy blocks it for a human; strict mapping accuracy 0/10.
3. The fault-specific verification never had to catch a bad fix in the live trials because every
   executed fix was correct; the health-only false-pass this run measures is 0. Its benefit shows
   when fixes are wrong (stage 2 baselines) and on the held-fault demonstrations (disk, network alias).
4. Disk: the model escalated by itself 9/10 here (override only needed once).

## Limits
n=10 per fault is small (intervals above); one machine, one model pair, temperature 0 (repeats are
near-deterministic, so variance across trials mostly comes from the environment); the knowledge
bundle used was learned from earlier development runs of the same faults (the eval trials are not
recorded into it); OOM/CPU/disk fixes are human-only so "recovery" is not measured for them.

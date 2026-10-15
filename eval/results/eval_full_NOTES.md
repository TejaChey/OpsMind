# Evaluation stage 1, re-run: full OpsMind on real injected faults (2026-10-15)

Re-run of the whole pipeline after `INCREASE_CPU_LIMIT` was added to the slow tier's menu
(`cpu_menu_NOTES.md`). The previous run is kept as `archive_run2_*` (menu without a CPU item).
Data: `eval_runs.jsonl` (70 jobs: 10 quiet controls + 10 trials x 6 faults), `eval_summary_full.csv`,
`eval_summary_full_overall.json`. Runner `eval/run_eval.py`, independent ground truth
`eval/ground_truth.py`. 0 harness errors, 0 excluded trials. Live actions only for the
user-approved kinds (network, dependency, config), as before.

## Headline numbers (valid trials = 10 per fault)
* Detection recall: 60/60 (each fault 10/10; Wilson 95% CI 0.72-1.0 per fault). Unchanged.
* Model alone (before the code floor): 10/60 (was 9/60): disk 10/10 by itself this time, every other
  fault 0/10. The floor overrode the model in 50/60.
* False positives on quiet controls: 0/10 (95% CI 0-0.28); model alone also 0/10.
* Mapping accuracy, STRICT ground truth: 50/60. All 10 misses are CPU, as before.
* Mapping accuracy INCLUDING the documented human-only alternative (CPU limit increase, see
  `cpu_menu_NOTES.md`; a post-hoc secondary score, always shown next to the strict one): 60/60.
  What changed for CPU: the proposal is now `INCREASE_CPU_LIMIT` 10/10 (was `INCREASE_MEMORY_LIMIT`
  10/10); it is blocked for a human by policy either way.
* Auto-fix (network, dependency, config; live): 30/30 recovered per INDEPENDENT ground truth;
  verifier false-pass 0/30; health-only false-pass 0/30. Unchanged.
* Disk proposals: INCREASE_DISK 7, UNKNOWN_NEEDS_HUMAN 3 (both accepted, human-only).
* Latency (medians): fast tier 0.4-0.7 s, slow tier 2.2-3.8 s; ~700 fast / 1000-1400 slow tokens.
  Time to recover: 9.0 s dependency, 12.1 s config, 17.9 s network.
  Time to detect: 0.5 s dependency, 5.9 s disk, 6.2 s config, 11.0 s network, 22.3 s CPU, 41.0 s OOM.

## Findings, not smoothed over
1. The fast model alone is poor at detection; the deterministic floor is what makes it work.
   Its 0/10 false-positive rate rests on only 10 controls.
2. Menu-dependence again: the CPU fix is only proposed once the menu offers it.
3. The fault-specific verification never had to catch a bad fix in the live trials (every executed fix
   was correct). Its benefit is shown separately on held faults: `verification_wrong_fix_NOTES.md`.

## Limits
n=10 per fault; one machine, one model pair, temperature 0 (repeats are near-deterministic); the
knowledge bundle was learned from earlier development runs of the same faults (eval trials are not
recorded into it); OOM/CPU/disk fixes are human-only so recovery is not measured for them. The
standing regression gate was not re-run after this evaluation (it passed on the same prompt before).

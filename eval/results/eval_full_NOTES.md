# Evaluation stage 1, run 4: full OpsMind on real injected faults, Redis present (2026-11-02)

Re-run after Phase 1 added a Redis cache as a second dependency of the web app (and a 7th fault,
`cache-down`). The signals now carry the cache facts, so the slow tier's cache prompt/menu text is
exercised on EVERY fault, which the standing regression gate (it replays stored signals) could not do.
The previous run is kept as `archive_run3_*` (6 faults, no cache). Data: `eval_runs.jsonl` (80 jobs: 10 quiet
controls + 10 trials x 7 faults), `eval_summary_full.csv`, `eval_summary_full_overall.json`. Runner
`eval/run_eval.py`, independent ground truth `eval/ground_truth.py`. 0 harness errors, 0 excluded trials.
Live actions only for the user-approved kinds (network, dependency incl. the cache, config).

## Headline numbers (valid trials = 10 per fault)
* Detection recall: 70/70 (each fault 10/10; Wilson 95% CI 0.72-1.0 per fault).
* Model alone (before the code floor): 10/70, all of it disk (10/10 by itself); every other fault 0/10.
  The floor overrode the model in 60/70.
* False positives on quiet controls: 0/10 (95% CI 0-0.28); model alone also 0/10.
* Mapping accuracy, STRICT ground truth: 60/70. All 10 misses are CPU (the model proposes
  `INCREASE_CPU_LIMIT` 10/10; the strict truth is "terminate the query").
* Mapping INCLUDING the documented human-only alternative (CPU limit increase; post-hoc secondary
  score, always shown next to the strict one): 70/70.
* Auto-fix (network, dependency, cache, config; live): 40/40 recovered per INDEPENDENT ground truth;
  verifier false-pass 0/40; health-only false-pass 0/40.
* New fault `cache-down`: detected 10/10 (model alone 0/10), RESTART_DEPENDENCY 10/10, executed and
  verified 10/10, truly recovered 10/10. The healthy database was never restarted (guard + code-chosen target).
* Disk proposals: INCREASE_DISK 10/10 (previous run: 7 + 3 UNKNOWN).
* Latency (medians): fast tier 0.7 s, slow tier 2.7 s. Time to recover: 8.9 s dependency, 12.2 s config,
  13.2 s cache, 29.0 s network. Time to detect: 0.6 s dependency, 6.0 s disk, 6.1 s config, 10.3 s cache,
  22.4 s CPU, 22.5 s network, 41.1 s OOM.

## Compared with the previous run (same machine, same models)
* Detection, auto-fix, verifier and false-positive results are unchanged in kind (60/60 -> 70/70 with the new fault).
* The 6 original faults' mapping is unchanged: 10/10 each except CPU (0/10 strict), so adding the cache
  text to the prompt did not break any older fault in production configuration.
* **Network partition got slower**: time to detect 11.0 s -> 22.5 s, time to recover 17.9 s -> 29.0 s. I did not
  investigate the cause; the extra container-state lookups cost well under a second, so this is NOT explained
  by the new code alone (machine load is possible). Treat latency numbers as indicative.

## Findings, not smoothed over
1. The fast model alone is poor at detection; the deterministic floor is what makes it work (10/70 vs 70/70).
2. Menu-dependence again, and the knowledge layer carries a lot (see the baselines notes): without it the
   network fault is 0/10 correct, config 2/10, OOM 1/10.
3. The fault-specific verification never had to catch a bad fix in the live trials (every executed fix
   was correct). Its benefit is shown separately on held faults: `verification_wrong_fix_NOTES.md`.
4. The summary needs TWO commands: `summarize_eval.py` (per-fault table) and `summarize_eval.py --compare`
   (comparison table). The per-fault CSV was stale for a while after the first run of the chain, which is how
   this was noticed; CLAUDE.md now lists both.

## Limits
n=10 per fault; one machine, one model pair, temperature 0 (repeats are near-deterministic); the knowledge
bundle was learned from earlier development runs (it now includes two cache-down runs; the eval trials are
not recorded into it) so `cache-down` benefits from precedent like the other faults; OOM/CPU/disk fixes are
human-only so recovery is not measured for them. The cache-down fault is one more instance of "dependency
stopped", so it is a modest test of cascading failure (the cause is in a different container than the symptom,
and a second possible cause must be ruled out), not a broad one.

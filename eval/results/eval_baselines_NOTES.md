# Evaluation stage 2, run 4: baselines and ablations (2026-11-02)

Same 80 saved signals (70 fault trials + 10 controls) from the Redis-present live evaluation. Offline rows
measure decision quality, NOT fix success. Data `eval_runs_offline.jsonl`, `eval_comparison.csv`; code
`eval/run_baselines.py`. Previous run: `archive_run3_*`.

| config | recall | false-pos | mapping strict | mapping incl. alt | wrong named action | wrong action policy lets RUN |
|---|---|---|---|---|---|---|
| full (live) | 70/70 | 0/10 | 60/70 (86%) | 70/70 | 10 | **0** |
| single_7b (always-on, no tiers/floor/knowledge) | 17/70 (24%) | 0/10 | 8/70 | 8/70 | 2 | 2 |
| fixed_rules (no LLM, restart-first + a cache rule) | 70/70 | 0/10 | 30/70 (43%) | 30/70 | 40 | 40 |
| no_floor (model alone decides) | 10/70 (14%) | 0/10 | 10/70 | 10/70 | 0 | 0 |
| no_knowledge | 70/70 | 0/10 | 33/70 (47%) | 43/70 (61%) | 36 | 17 |
| no_policy_gate | 70/70 | 0/10 | 60/70 | 70/70 | 10 | 10 |
| health_only_verification (live fixes) | same as full | | | | false-pass 0/40 | |

## Notes on this run
* **Fixed rules got one extra rule for the cache** ("cache container down -> start it"), as a runbook author
  would add when Redis joined the stack; without it that baseline restarts the healthy database. With it,
  fixed rules get `cache-down` and `dependency-down` right and are wrong everywhere else (40 wrong actions
  that policy would run).
* **single_7b detected 17/70 (24%)**: it is sensitive to prompt text (27/60 before the CPU menu item, 10/60
  after it, 17/70 now). Report it as a range across menu versions, not one number. Unlike the previous run it
  now has 2 wrong actions policy would let run (previously 0).
* **Knowledge carries much of the mapping accuracy.** Without it: network 0/10, config 2/10, OOM 1/10,
  CPU 0/10, while dependency, disk and cache-down are 10/10. Production (knowledge on) is 10/10 on every fault
  except CPU. `cache-down` is 10/10 even without knowledge: the cache facts and the menu item are enough.
* Floor: removing it drops recall 100% -> 14%.

## What each result does and does not show
* Policy gate: with it 0 wrong actions can run; without it the 10 CPU proposals could (they have no executor, so
  no real harm: a policy-level counterfactual).
* Fault-specific verification: not demonstrated by the live trials (0/40 either way); see
  `verification_wrong_fix_NOTES.md`.

## Limits
n=10 per fault; one machine and model pair; temperature 0; baselines are one reasonable implementation each
(the fixed-rule set is deliberately generic and restart-first); offline configurations cannot show recovery;
the "incl. alt" column is a post-hoc secondary score.

# Evaluation stage 2, re-run: baselines and ablations (2026-10-15)

Same 70 saved signals (60 fault trials + 10 controls) from the re-run live evaluation. Offline rows
measure decision quality, NOT fix success. Data `eval_runs_offline.jsonl`, `eval_comparison.csv`;
code `eval/run_baselines.py`. Previous run: `archive_run2_*`.

| config | recall | false-pos | mapping strict | mapping incl. alt | wrong named action | wrong action policy lets RUN |
|---|---|---|---|---|---|---|
| full (live) | 60/60 | 0/10 | 50/60 (83%) | 60/60 | 10 | **0** |
| single_7b (always-on, no tiers/floor/knowledge) | 10/60 (17%) | 0/10 | 10/60 | 10/60 | 0 | 0 |
| fixed_rules (no LLM, restart-first) | 60/60 | 0/10 | 20/60 (33%) | 20/60 | 40 | 40 |
| no_floor (model alone decides) | 10/60 (17%) | 0/10 | 10/60 | 10/60 | 0 | 0 |
| no_knowledge | 60/60 | 0/10 | 26/60 (43%) | 36/60 (60%) | 34 | 15 |
| no_policy_gate | 60/60 | 0/10 | 50/60 | 60/60 | 10 | 10 |
| health_only_verification (live fixes) | same as full | | | | false-pass 0/30 | |

## What changed since the previous run, and why it matters
The only code change between the two runs is the new CPU item on the slow tier's menu. It moved
results outside CPU:
* **single_7b recall fell from 27/60 (45%) to 10/60 (17%).** This baseline reuses the slow-tier
  facts and menu, so it inherits the menu's wording. The baseline is therefore sensitive to prompt
  text; report the range (45% and 17% for two menu versions), not one number.
* **no_knowledge, same total, different composition:** 26/60 strict both times, but OOM without
  knowledge fell from 7/10 correct to 1/10 (the model now proposes `INCREASE_CPU_LIMIT` 9/10 for a
  memory kill), while dependency stays 0/10, config fell 9/10 -> 5/10, network rose 1/10 -> 10/10 and
  disk is 10/10 (UNKNOWN accepted). Production (knowledge ON) is unaffected: full config is 10/10 on
  OOM and every other fault, so the production answer for OOM now leans on the knowledge layer.
* "Knowledge lifts mapping accuracy" therefore reads 43% -> 83% strict (26 -> 50 of 60) here as
  before, but the old per-fault story (network and dependency only) no longer holds: the gain is now
  spread over config, OOM and dependency, and the knowledge-off model is less stable than it looked.

## What each result does and does not show
* The floor is what makes detection work: removing it drops recall 100% -> 17%.
* Fixed rules detect everything but name the wrong fix 40/60 times and policy would let all 40 run
  (a deliberately generic restart-first rule set; a hand-tuned per-fault set would match by design).
* Policy gate: with it 0 wrong actions can run; without it the 10 CPU proposals could (they have no
  executor, so no real harm: a policy-level counterfactual).
* Fault-specific verification: not demonstrated by the live trials (0/30 either way); see
  `verification_wrong_fix_NOTES.md`.

## Limits
n=10 per fault; one machine and model pair; temperature 0; baselines are one reasonable
implementation each; offline configurations cannot show recovery; the "incl. alt" column is a
post-hoc secondary score.

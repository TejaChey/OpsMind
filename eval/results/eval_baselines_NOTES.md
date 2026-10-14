# Evaluation stage 2: baselines and ablations (2026-10-14)

All configurations replay the SAME 70 saved signals (60 fault trials + 10 quiet controls) from the live
full-configuration run. Offline rows measure decision quality; they do NOT measure fix success (nothing
is executed), which is only measured for `full` (live, independent ground truth). Data:
`eval_runs_offline.jsonl`, `eval_comparison.csv`; code `eval/run_baselines.py`.

| config | recall | false-pos | mapping acc | wrong named action | wrong action policy lets RUN |
|---|---|---|---|---|---|
| full (live) | 60/60 | 0/10 | 50/60 (83%) | 10 | **0** |
| single_7b (always-on, no tiers/floor/knowledge) | 27/60 (45%) | 0/10 | 10/60 (17%) | 16 | 16 |
| fixed_rules (no LLM, restart-based) | 60/60 | 0/10 | 20/60 (33%) | 40 | 40 |
| no_floor (model alone decides) | 9/60 (15%) | 0/10 | 9/60 (15%) | 0 | 0 |
| no_knowledge | 60/60 | 0/10 | 26/60 (43%) | 21 | 20 |
| no_policy_gate | 60/60 | 0/10 | 50/60 | 10 | 10 |
| health_only_verification (live fixes) | same as full | | | | false-pass 0/30 |

## What each result does and does not show
* **The floor is what makes detection work**: removing it drops recall 100% -> 15%. The always-on 7b
  model alone reaches 45%: it never escalated OOM, CPU or disk ("No clear issue identified") and
  mis-diagnosed network/dependency incidents.
* **Fixed rules detect everything but fix the wrong thing**: a restart-first runbook names the right
  action 33% of the time, and in 40/60 trials policy would let a wrong action (mostly restart) run.
  Caveat: this is a deliberately generic rule set (documented in `fixed_rules()`), not tuned per
  fault; a hand-tuned per-fault rule set would, by construction, match the ground truth.
* **Policy gate**: with it, 0 wrong actions can run; without it, the 10 wrong CPU proposals could.
  (They have no executor today, so no actual harm: this is the policy-level counterfactual.)
* **Knowledge** lifts mapping accuracy 43% -> 83% (network 1/10 -> 10/10, dependency 0/10 -> 10/10).
  Note two things: the bundle was learned from earlier development runs of the SAME faults, and for
  OOM (7/10 -> 10/10) there is no verified experience at all, only the authored description/signals
  text, so that gain is not "learning from experience".
* **Fault-specific verification**: in the live trials every executed fix was correct, so a plain
  health check never false-passed (0/30). This run does NOT demonstrate the benefit; the held-fault
  demonstrations earlier (disk: health-only said verified while writes still failed; network: DNS
  alias missing) do. A fair test needs wrong fixes executed live, which is not approved for
  restart-style actions.

## Findings discovered while evaluating
1. All production signals now carry `last_deploy` (attached whenever deploy history exists), so the
   "only offer rollback when deploy facts are present" prompt change is inert in practice, and the
   standing regression gate used to pass on older stored signals that production no longer sees.
   The gate now also replays the 60 live evaluation incidents (95 total) and passes.
2. CPU spike stays unsolved by the slow tier: proposals are "increase the memory limit" 10/10 (wrong
   named action, human-blocked by policy). Gate threshold for cpu-spike is 0.0 as a documented
   known finding.

## Limits
n=10 per fault; one machine and model pair; temperature 0; baselines are one reasonable
implementation each (the single-7b prompt reuses the slow-tier facts/menu with an added `escalate`
field); offline configurations cannot show recovery.

# CPU-limit action added to the slow tier's menu (2026-10-15)

Change: new action `INCREASE_CPU_LIMIT` (mapper rule, policy.yaml `auto_approve: false`, no executor,
human-only) and the plain-language menu item "increase the container's CPU limit" in
`agents/slow_tier.py`. Motivation: the live eval's CPU misses (10/10 proposed "increase the MEMORY
limit") came from a menu that had a memory item and no CPU item (same menu-dependence as the network
and rollback faults).

Measured with `python eval/slow_tier_regression.py` (95 real incidents replayed through the 7b slow
tier, knowledge on, temperature 0):

| fault | before (menu without CPU) | after |
|---|---|---|
| cpu-spike (n=10) | 10/10 proposed INCREASE_MEMORY_LIMIT | 10/10 propose INCREASE_CPU_LIMIT |
| oom 16, disk 11, network 18, dependency 15, config 25 | 100% correct | 100% correct (unchanged) |

Remapping every proposal stored in the repo with the old and new mapper: 0 of 22 distinct proposals
change, so existing behaviour is untouched.

## What this does and does not show
* Scoring is NOT changed: the ground truth for cpu-spike is still `TERMINATE_QUERY`, so strict CPU
  mapping accuracy is still 0/10. The model now names a different, defensible action (a capacity
  change, blocked for a human, the same kind of fix accepted as correct for OOM and disk). Whether
  to count it as correct is a scoring decision made AFTER seeing results; if adopted, report both
  numbers and say so.
* Replay measures the slow tier on the saved signals only. The headline 50/60 mapping and the
  baseline comparison were measured with the old menu and have not been re-run.
* n=10, one model, deterministic; the 10 CPU signals are near-identical, so this is closer to one
  observation than ten independent ones.
* Rule 4 check: the menu item describes an action that exists in the mapper and policy; the model
  is still free to propose anything, and nothing forces it onto an automatable action.

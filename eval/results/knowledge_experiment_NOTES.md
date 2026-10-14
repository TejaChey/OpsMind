# Knowledge experiment: results and limits (2026-10-14)

Data: `knowledge_experiment_raw.jsonl` (126 slow-tier calls, every one `source=model`),
`knowledge_experiment_summary.csv`, script `eval/knowledge_experiment.py`.
Replay of the 27 escalated real incidents in `trajectories/runs.jsonl`; slow model qwen2.5:7b,
temperature 0 (one pass per incident x condition). Scored in code against the ground-truth action.
CPU is absent (no real CPU incidents in runs.jsonl). Disk has n=1 (not interpretable).

## Results (correct / n)
| fault | control_full | know_full | control_ablated | know_ablated | know_misleading |
|---|---|---|---|---|---|
| config-regression | 8/8 | 8/8 | 1/8 | 8/8 | 0/8 |
| dependency-down | 0/5 | 5/5 | 0/5 | 4/5 | 0/5 |
| network-partition | 0/7 | 7/7 | 0/7 | 6/7 | 0/7 |
| oom | 6/6 | 6/6 | 0/6 | 0/6 | - |

## Findings
1. **The slow tier is fragile to prompt text, and the current prompt has regressed Faults 4 and 5.**
   `control_full` is 0/7 (network) and 0/5 (dependency) with today's prompt, but the same
   incidents were answered correctly live. Bisecting the real historical prompts (git) with the
   same incident and temperature 0: the Fault 4/5 prompts give the original correct answers; the
   Fault 6 prompt (added deploy-facts text + rollback menu item) flips both to wrong answers.
   Removing only the rollback item did not restore them. No slow-tier regression check was run
   when the prompt changed (only the mapper was re-checked): a process gap.
2. **With past verified incidents shown, the model follows the precedent**: it restores the
   correct answer on the regressed faults (network 0/7 -> 7/7, dependency 0/5 -> 5/5), keeps
   config at 8/8, and does not hurt OOM (6/6, no experience exists for it).
3. **Experience can stand in for a missing menu item**: with the correct remedy removed from the
   menu, knowledge gives config 1/8 -> 8/8, network 0/7 -> 6/7, dependency 0/5 -> 4/5.
4. **Knowledge cannot help where no verified experience exists**: OOM ablated stays 0/6 (it
   proposes a restart every time).
5. **Harm check: the model follows the precedent whether or not it fits.** Given the WRONG fault
   type's file, 20/20 proposals copied that file's remedy (network->restart dependency,
   dependency->rollback config, config->reconnect network). So the gain in (2)/(3) is "copying
   the matching precedent", not independent reasoning, and it is only as safe as the code that
   picks the matching file (`knowledge.matched_faults`).

## Limits (do not overclaim)
* Single pass at temperature 0, small n, one model, one machine; no confidence intervals.
* Every precedent is the same variant of its fault (e.g. only the `db_port` config fault), so
  generalisation to a novel variant is untested.
* "wrong_would_auto_execute" means policy.yaml would let that (wrong) action run, not that it
  causes damage: e.g. RECONNECT_NETWORK on a dependency incident is stopped by the executor's
  no_target guard, and a wrong restart is caught by verification.
* Leave-one-out removes the replayed run's own outcome, but other runs of the same fault class
  remain as precedent, which is the intended "learning from experience" setting.

---

# Generalisation test: a new variant the knowledge had never seen (2026-10-14)

Real fault, not a replay: `faults/config_regression.py --variant host` deploys a typo'd database
host (`postgress`). The knowledge bundle held only the `db_port` variant of this fault. The app's
error ("could not translate host name") also resembles the dependency-down error. Dry-run
supervisor, 3 runs per condition, production prompt, slow model qwen2.5:7b, temperature 0.

| condition | proposal (3/3 each) | mapped | correct? |
|---|---|---|---|
| knowledge ON (production default) | "Roll back the web app's config to the last known-good deploy" | ROLLBACK_CONFIG | yes |
| knowledge OFF (`--no-knowledge`) | "Reconnect the container to its Docker network" | RECONNECT_NETWORK | no |

Notes: with knowledge OFF the diagnosis itself was right ("DNS resolution failure for the
database") but the remedy was wrong; the executor's `no_target` guard made it harmless, but the
incident stays unresolved with no human flagged. One live run with knowledge ON then rolled the
config back (`db_host` postgress -> postgres), passed verification (app 200 + live config equals
known-good), and was recorded in the knowledge bundle automatically. The standing regression
gate still passes with these incidents added (35 real incidents, all faults 100%).

Limits: n=3 per condition (deterministic at temperature 0, so repeats add little); ONE new
variant of ONE fault class, whose deploy facts have the same shape as the known variant, so this
is *near* generalisation. Not tested: a brand-new fault type, a within-class case where the
precedent's remedy would be wrong, or a variant whose signals match a different fault node.
Consistent with experiment finding 5, the model most likely copies the matching precedent's
remedy; here that is the right thing, which is exactly the situation where it helps.

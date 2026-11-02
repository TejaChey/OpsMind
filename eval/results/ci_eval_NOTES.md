# Phase 2: CI-pipeline triage (second domain), results and limits

Run 2026-11-02, `python ci/run_ci_eval.py --trials 5 --live`; raw rows `ci_eval_runs.jsonl`,
table `ci_eval_summary.csv` (Wilson 95% CIs). 20 trials (3 faults x 5 + 5 green controls), 0 harness errors.
Same architecture as the Docker domain (signal -> fast tier + floor -> slow tier -> mapper ->
policy -> executor -> verification), pointed at a real local pipeline whose steps really fail.

| metric | result |
|---|---|
| detected, full system (floor) | 15/15 |
| detected, fast model alone | 0/15 (the floor is essential again) |
| false positives on green controls | 0/5 |
| mapped correctly, flaky-network (RERUN_PIPELINE) | 5/5 |
| mapped correctly, missing-dependency (FIX_DEPENDENCY, human-only) | 5/5 |
| mapped correctly, test-regression (strict: REVERT_COMMIT) | 0/5 (proposed FIX_CODE 5/5) |
| ...incl. alternative FIX_CODE (post-hoc, secondary) | 5/5 |
| auto-fixes executed and truly recovered (independent ground truth) | 5/5 |
| verifier false-passes | 0/5 |
| wrong actions that actually executed | 0/15 |
| wrong actions a "re-run on red" rule would run | 10/15 |

## What it shows
* The fast model never escalated a red pipeline by itself; a deterministic floor did (as with Docker).
* The retry trap: `missing-dependency` is the same commit that was green before, which tempts a
  re-run; the model did not fall for it (FIX_DEPENDENCY 5/5) and the executor would have refused
  a re-run anyway (error class is deterministic). A fixed retry-on-red rule is right only for the flaky fault.

## Findings / limits (read before quoting numbers)
* **Strict 0/5 on test-regression is a model preference, not a bug**: the menu offers "revert the most
  recent commit" and the facts say `commits_since_green = 1`, yet the model proposed fixing the code by hand
  (human-only). Same pattern as the CPU spike. Alternative scoring was fixed before the trials but after a
  dry run showed the preference, so it is post-hoc: report strict and alternative together. The prompt was not tuned.
* `REVERT_COMMIT` therefore never ran from a model proposal. It was exercised separately on real git with
  the model bypassed (a labelled harness check, not a result): dry-run named the target, live revert executed,
  a fresh pipeline went green, tree == last-green tree, independent ground truth agreed; with 2+ commits after
  green the guard returned `no_target`. The `test-regression` "recovered" flag in the raw rows is False
  because nothing was executed (human-only), which is expected.
* **Not real GitHub Actions logs**: GitHub serves job logs only to an authenticated caller (checked: 403
  without a token), so this uses a local pipeline we run ourselves. Faults are injected, so ground truth is
  known by construction; it is not a sample of real-world CI failures. The "generalises to a second domain"
  claim rests on 3 synthetic fault types.
* n=5 per fault, one machine, deterministic models (temperature 0), so the trials are not independent
  samples; intervals are wide. The flaky outage is a one-shot 503 that clears itself by design.
* The CI domain has no knowledge layer and no standing regression gate yet.

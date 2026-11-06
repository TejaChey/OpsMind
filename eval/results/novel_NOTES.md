# Novel faults, and the agent vs the pipeline (2026-11-06)

**Questions.** (1) Does OpsMind survive failures it was not built for? (2) Is an *agent*
(the model chooses read-only tools in a loop, `agents/investigator.py`) better than the
production *workflow* (code collects fixed facts, the model answers once)?

**Method.** `eval/novel_faults.py`: real faults, held while both reasoners run on the SAME
incident (shared signal + fast tier), DRY RUN only (nothing executed), scored in code.
Novel faults (`faults/novel.py`): connection exhaustion, read-only database, frozen (paused)
app, two faults at once (cache down + bad deploy). Known faults: database down, cache down,
network partition, bad config deploy (their existing correct actions). n = 3 per fault and
round. Rules in `eval/novel_ground_truth.py` were written before any run; tables from
`python eval/summarize_novel.py <file>`.

**Scoring caveat (read this).** Three pre-registered diagnosis patterns turned out to be
wrong: `port` matched "re**port**s", `clients` matched a network diagnosis, and `read[- ]?only`
rejected the setting name `transaction_read_only`. The **action** scores (what the system would
do) are unaffected. A clearly labelled POST-HOC strict diagnosis score is printed next to the
pre-registered one; both are reported. Diagnosis numbers below are the strict post-hoc ones.

## Round 1, before any fix (`novel_runs.jsonl`, 4 novel faults x 3)

| | pipeline | agent |
|---|---|---|
| detected | 9/12 (frozen app missed 3/3) | 9/12 (same: detection is shared) |
| correct diagnosis | **0/12** | **7/12** |
| wrong action that would auto-run | 0 | 0 |

What broke, found by reading the logged trajectories:
1. **Detection gap:** a paused app's health check times out, and the floor read "no answer"
   as "not measured". Fixed in `fast_tier.apply_floor`: an app that was probed and did not
   answer, or whose container is not running, escalates (mutation-tested). On the 80 stored
   eval signals: still 70/70 faults, still 0/10 false positives.
2. **Wrong precedent:** the code-side knowledge matching over-matched. The read-only database
   was shown the *disk-full* incident (a failed write) and "quoted" `No space left on device`
   3/3; connection exhaustion was shown the *network-partition* incident and blamed the network
   3/3. Fixed in `knowledge.matched_faults`: a failed write is disk evidence only with a space
   error; a failed connect is a partition only if the server did not answer (a FATAL reply means
   the network works). On all 131 stored real and eval signals the matching is unchanged; it
   changed only for the 6 novel ones. Standing gate after the change: PASS.

## Round 2 (`novel_runs_after_fixes.jsonl`): the two fixes alone

The plain pipeline went from 0/3 to 3/3 correct on read-only and from undetected to 3/3 on the
frozen app. Most of the round-1 gap was the code around the model, not the model.

## Round 3, both fixes plus agent hardening (`novel_runs_v2.jsonl`, 8 faults x 3)

Agent hardening (none of it gives the answer): logs limited to the last 3 minutes (stale lines
from the previous trial misled it), a limit on repeated identical calls, and the code-picked
evidence list also applied to the out-of-budget conclusion.

| | pipeline | agent | cross-check (act only if both agree) |
|---|---|---|---|
| known faults: correct action | **12/12** | 8/12 (network 0/3, cache 2/3) | 8/12 |
| novel faults: correct action | 9/12 (conn-exhaust 0/3) | 10/12 | **12/12** |
| correct diagnosis (strict) | 17/24 | 19/24 | – |
| wrong action that would auto-run | **0/24** | **0/24** | 0/24 |
| median seconds | 3–5 | 16–68 | both |
| invalid output (fallback) | 0 | 2/24 | – |

## What this says
- **Safety held in all 84 decisions** across the three rounds (90 reasoner rows; the other 6
  are the undetected frozen app in round 1, where nothing was decided): no wrong action would
  have run on its own, whichever reasoner was used. It comes from the code (mapper to a
  fixed menu, policy, code-chosen targets, `no_target` guards), not from the model being right.
- **With this model (qwen2.5:7b, local), the agent is not better overall.** It is better when the
  cause needs looking up (connection exhaustion: 3/3 vs 0/3 actions) and worse on known faults
  (it never proposed the network reconnect 0/3, and once invented a cause for a stopped cache).
  It is 5–20x slower and sometimes ignores evidence it fetched itself.
- **Cross-check** makes the right call on every novel incident, but on known faults it hands
  4/12 auto-fixable incidents to a human: safer, but less automation. A trade-off for the
  operator to choose, not a free win.
- **Production default stays the pipeline.** `--investigate` (agent) and `--cross-check` are
  opt-in flags (also on the alert receiver), with the trade-offs above.
- **Monitoring blind spot:** the read-only database never trips standard monitoring (`/health`
  stays 200, 0/9 alerts) but OpsMind's own write probe catches it every time (9/9 detected).

## Limits
n = 3 per fault per round, one machine, one model size; the agent's quality is bounded by a
7B local model (a stronger model may change the agent result; untested). The fixes from round 1
were made after seeing round-1 failures on these same fault types, so rounds 2–3 are not an
independent test of the fixes. Novel faults are four, hand-picked. All dry run: fix success was
not measured here (the verifier was shown to refuse a fix that fixed nothing whenever an action
would have run).

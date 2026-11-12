# OpsMind v2 (multi-agent graph) against the v1 pipeline (2026-11-12)

Code: `opsgraph/` (graph), `eval/graph_eval.py` (harness), scoring rules in
`eval/novel_ground_truth.py` (written before the runs). Data: `graph_eval_dry.jsonl` (v2b,
paired), `graph_eval_dry_v2a.jsonl` (stopped run, kept), `graph_eval_live.jsonl`. Every graph
run's full event stream is in `trajectories/graph/<run_id>.jsonl` and can be replayed in the
console. Models: qwen2.5:3b (triage) and qwen2.5:7b (all other agents), temperature 0, one
laptop (Apple M5, 16 GB).

## What changed during development (all found on real faults, in this order)

| Found | How | Fix |
|---|---|---|
| Database agent called a healthy database faulty | live two-fault test: it had read the web app's error text about port 5439 | each specialist sees only its own component's measurements |
| Commander ignored a stopped cache that an agent had found with evidence | same test | code review: every measured fault needs a step |
| Critic model approved that plan | same test | code objections cannot be overruled by the model |
| Commander labelled a network fix as the cache fix | dry re-run | code review: a step's action must act on its component |
| Correct config rollback reported "failed" | live two-fault test: it ran while the cache was still down | code orders steps: dependencies and networks before config |
| Every incident treated as a config problem | first evaluation attempt: the previous rollback had been labelled "bad" | app counts as a cause only if the live config differs from the last good one |
| Agents misled by that mislabelled deploy | second attempt | recorded a fresh good baseline deploy before the clean runs (environment, not code) |
| Critic model vetoed correct plans with untrue objections (3 of 3 blocks in v2a) | v2a run, stopped after 17 trials | v2b: the critic model is advisory, only code objections block |
| Agent opinions created impossible requirements (DB "faulty" during a network partition) | v2a | v2b: coverage is required only for faults code measured directly |
| "Reconnect the PostgreSQL container to the services_default network" failed closed | v2b dry run | mapper window widened; reconnecting the app goes to a person; 10 of 112 stored proposals map differently, all "reconnect ... network" (applies to the live run, not to the v2b dry run) |

## v2b dry run: same incidents, both systems (8 fault types x 3)

| | v1 pipeline | v2 graph |
|---|---|---|
| detected | 24/24 | 24/24 |
| correct diagnosis | 17/24 | **19/24** |
| known faults: every proposed action correct | **12/12** | 8/12 |
| known faults: the automatic steps are correct | **12/12** | 7/12 |
| unseen faults: correct decision | 9/12 | 9/12 |
| wrong action that would run on its own | 0/24 | 0/24 |
| median seconds per incident | 4 to 5 | 36 to 50 |

Per fault (correct decision, pipeline vs graph): database down 3 vs 2, cache down 3 vs 3,
network partition 3 vs 0 (the mapper gap above), config 3 vs 3, connection exhaustion
**0 vs 3**, read-only 3 vs 1, frozen app 3 vs 2, two faults 3 vs 3 (both partial: neither planned
the full two-step fix in this run).

## Live: the graph acting on real faults (5 fault types x 3, governor off so trials can repeat)

Recovery is judged independently of the verifier (the fault's own presence check says gone AND
the app answers 200 with both dependencies OK).

| fault | recovered by the graph alone | note |
|---|---|---|
| database down | 3/3 | |
| cache down | 2/3 | once the commander gave confidence 0.5 and policy held it for a person |
| bad config deploy | 3/3 | |
| network partition | 1/3 | twice the plan went to a person |
| two faults at once | 0/3 | once it restarted the cache and held the rollback for a person: the verifier correctly said "not verified" |
| **total** | **9/15** | **verifier false passes 0, wrong actions executed 0** |

Every one of the 9 verified runs was confirmed recovered by the independent check, and every
run the verifier did not pass was indeed not recovered.

## v2a (stopped after 17 of 24 trials; critic model could block)

v1 pipeline: known 9/9 correct, unseen 6/8. Graph: known 5/9, unseen 8/8. Wrong automatic
actions 0 for both. The graph's known-fault losses were mostly the critic model blocking
correct plans, which is why v2b made it advisory.

## What this shows

1. **Safety came from the code in every configuration.** Across v2a, v2b and live, 56 graph
   incidents and 41 pipeline incidents, no wrong action ran or would have run on its own.
2. **More agents did not make a 7B model better at choosing fixes.** The single-call pipeline
   chose the right automatic fix more often on known faults (12/12 vs 7/12) and was about 10x
   faster. Each extra agent is one more place where the chain can fail closed.
3. **The graph is better at explaining.** It diagnosed more incidents correctly (19 vs 17 of 24)
   and was the only system to recognise connection exhaustion (3/3 vs 0/3), because a specialist
   could query the connection count.
4. **An LLM critic is a poor gatekeeper.** It approved plans the code rejected and vetoed correct
   ones with objections that were not true. Code checks did the useful reviewing.
5. **Half the problems were in our own code.** Of the ten in the first table, five were in our
   code or environment (step order, the app-as-cause rule, the coverage rule, the mapper, a
   mislabelled deploy) and five were model behaviours now contained by a code rule. Every one
   was found only by running real faults; none showed up in unit tests.

Practical reading: with small local models, use the fast pipeline to act and the agent graph to
investigate and explain, especially for incidents the pipeline hands to a person. The console
makes the graph's evidence visible to that person.

## Limits

n = 3 per fault and round; one machine, one model family, temperature 0. The v2b fixes were
made after seeing the v2a results, and the mapper fix after the v2b dry run, so these are
successive design iterations measured on the same fault types, not independent tests. The live
run used the fixed mapper; the v2b dry run did not, so its network-partition result for the
graph (0/3) reflects the old mapper. The governor was off in the live run so trials could
repeat. "Two faults at once" is one hand-built combination.

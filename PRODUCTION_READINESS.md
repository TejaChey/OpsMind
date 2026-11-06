# Would a real company use OpsMind?

Written 2026-11-02 after reading practitioner guidance and published postmortems on
auto-remediation (sources at the end). Mostly opinion pieces and vendor blogs, so treat the
"industry" claims as direction, not measurement.

## Short answer

- **As a product today: no.** It runs on one laptop, against Docker containers it knows by
  name, with local models. A company would need it to read *their* signals (Prometheus,
  Datadog, Kubernetes events), act on *their* platform (Kubernetes, cloud APIs), sign in as a
  least-privilege identity, and page *their* on-call. None of that exists.
- **As a design: yes, the parts companies ask for are the parts OpsMind already has.** The
  guidance converges on: graduated autonomy (only narrow, reversible actions run alone; the
  rest go to a human), a policy gate, verification that does not trust the model, and a full
  audit trail. OpsMind has all four, measured on real injected faults (70/70 detected,
  40/40 automatic fixes truly recovered, 0 wrong actions executed).
- **Would it help?** For the faults it knows, yes: it diagnoses, picks a safe fix, proves the
  fix in code and records the evidence. Its honest weak spots are in the eval notes: the model
  is prompt-fragile, the knowledge layer helps a lot (and copies the wrong precedent when the
  matching is wrong), and only 7 fault types on one stack were tested.

## What the research said was missing (and what was added)

Practitioners' recurring story is automation making an outage *worse*: restart loops, a fix
re-applied over and over, a broken automation that had to be switched off by hand (AWS's
October 2025 DynamoDB DNS write-up; an autoscaling group that kept relaunching instances).
OpsMind judged every incident on its own, with no memory of what it had already done. A
vibe-coded agent does not think about this, because each single run looks correct.

| Gap | Risk | Added (2026-11-02) |
|---|---|---|
| No memory across incidents | Restart loop: the same fix re-runs every time the fault returns | `tools/governor.py`: a fix that **failed verification** in the last 30 min is never repeated automatically, and the same action on the same target runs at most **2 times per 30 min** (a returning fault means the fix treats a symptom). Decided in code from the logged live runs; unreadable history fails closed |
| No off switch | On-call cannot stop the automation fast | Kill switch: `touch OPSMIND_PAUSED` (or `automation_enabled: false` in policy.yaml). Triage still runs, nothing executes, every incident is handed to a human |
| Fixes destroy evidence | A restart wipes the logs that explain the crash, so nothing is learned | `tools/forensics.py`: container state + last 50 log lines saved into the trajectory right before any live action |
| Nobody is told | "Blocked pending human approval" was just a line in a log file | `tools/escalation.py`: blocked, refused, failed or unverified runs go to `trajectories/escalations.jsonl` with root cause, proposed fix, what was done and a link to the trajectory (where a pager/chat adapter would plug in) |

The governor is planned first and acted second: the executor first runs as a dry run (which
picks the real target in code), the governor judges exactly that target, and only then does
the live action run. The eval harness does not use the governor (each trial must stand alone).

**Update 2026-11-06, OpsMind now runs behind standard monitoring.** Prometheus + Alertmanager
(compose profile `monitoring`) alert on the app's new `/metrics`; Alertmanager calls
`tools/alert_receiver.py` (authenticated, deduplicated, allow-listed, bounded queue, one
incident at a time, alert text never shown to a model, every event in
`trajectories/alerts.jsonl`). The governor also has a **change freeze**: no automatic action
while a deploy's health is still undecided (`deploys/IN_PROGRESS`, written by `tools/deploy.py`).
On a real Redis fault repeated 3 times through this loop: fixes 1 and 2 executed, verified and
resolved the alert with no human; fix 3 was **refused by the repeat limit** and handed to a
human (`eval/results/alert_loop_NOTES.md`, n=3). The repeat limit and the evidence snapshot are
now shown live; the failed-fix rule and the freeze are still unit tests only.

**Evidence (2026-11-02).** 15 new unit tests. All 4 governor rules were mutation-tested (each deliberately
broken rule fails a test). One real run on a real fault: Redis stopped, kill switch on,
`--live` → diagnosed correctly, governor refused, Redis stayed down, hand-off queued
(run 324f9bbf). **Not yet shown live:** the repeat limit and the failed-fix rule (unit tests
only), and the snapshot on a real live action.

## Is it agentic, or automation? (2026-11-06)

Honest answer: the production path is an **agentic workflow**: code decides what to measure
and in what order, the model makes judgment calls at fixed points (escalate? what is wrong?
which fix?). That is deliberate: every decision that matters is checked by code. A true
**agent** now exists too: `agents/investigator.py`, where the model chooses read-only tools in a
loop until it knows the cause (opt-in: `--investigate`, or `--cross-check` to require the agent
and the workflow to agree before acting). On real failures it was never built for, and with a
local 7B model, the agent was better where the cause had to be looked up and worse on known
faults, 5–20x slower, and neither ever chose a wrong action that would have run
(`eval/results/novel_NOTES.md`).

## Surviving real-world failures (2026-11-06)

Tested on 4 real failure types it was never built for (connection exhaustion, read-only
database, frozen app, two faults at once). First result: it missed one completely (a frozen app)
and its diagnosis was wrong on all 12 incidents, because (1) the floor treated "no answer" as
"not measured" and (2) the knowledge layer showed it the wrong past incident, which it copied.
Both were code fixes; after them the plain pipeline diagnosed the read-only and frozen-app
cases 3/3. Across the 84 decisions no wrong action would have run. Still not handled: anything
OpsMind has no read-only view of (e.g. application code bugs, slow queries without errors,
gradual degradation), and it does not fix code: it restarts, rolls back, reconnects, or hands
to a human.

## Still missing before a company would trust it (not built)

1. **Integrations**: alert ingest from Alertmanager now exists (above), so OpsMind can sit
   behind a company's existing alerting. Still missing: reading their metrics/logs as signal
   (OpsMind measures its own facts from Docker) and a Kubernetes/cloud executor. The
   architecture (signal → tiers → mapped action → governor → executor → verifier) would not
   change; the adapters would.
2. **Identity and permissions**: the executor uses the local Docker socket, i.e. full control.
   A real deployment needs a separate, least-privilege identity per action type.
3. **Change awareness**: done for deploys made through `tools/deploy.py` (change freeze in the
   governor). Migrations and maintenance windows from a company's calendar are not read.
4. **Durability of a fix**: verification runs seconds after the fix. The repeat limit catches a
   fault that comes back, but a scheduled re-check (e.g. 10 min later) would catch it sooner.
5. **Shadow mode at scale**: companies adopt in steps (read-only, then propose with one-click
   approval, then narrow autonomy). Dry-run mode is the first step; what's missing is a report
   comparing proposals with what humans actually did.
6. **Scale and concurrency**: one supervisor, one host, sequential incidents.

## Sources

- [What SREs should automate and never automate with AI (HackerNoon)](https://hackernoon.com/what-sres-should-automate-and-never-automate-with-ai)
- [AI SRE safety architecture: guardrails, escalation paths, human control](https://www.softwareseni.com/ai-sre-safety-architecture-guardrails-escalation-paths-and-human-control)
- [AI SRE agents guardrails (DZone)](https://dzone.com/articles/ai-sre-agents-guardrails)
- [SRE AI agent safe-failure implementation (DEV)](https://dev.to/vpodk/sre-ai-agent-safe-failure-implementation-5gcj)
- [Human-in-the-loop SRE escalation policies (DEV)](https://dev.to/npayyappilly/the-human-in-the-loop-sre-designing-automation-escalation-policies-for-ai-assisted-operations-2c7f)
- [AWS outage post mortem: when automation bites back (Constellation Research)](https://www.constellationr.com/insights/news/aws-delivers-outage-post-mortem-when-automation-bites-back)
- [Anatomy of an outage: our autoscaling group "helping hand" (DevOps.com)](https://devops.com/anatomy-of-an-outage-our-aws-autoscaling-group-helping-hand-pushed-us-off-the-cliff/)
- [The autonomous remediation bill (DEV)](https://dev.to/zop_8abedcc7e12/the-autonomous-remediation-bill-0-saved-3-outages-created-4n2)
- [We automated the incident and lost the learning](https://platform-notes.pages.dev/blog/we-automated-the-incident-and-lost-the-learning)
- [Ciroos AI SRE (WorkOS)](https://workos.com/blog/ciroos-ai-sre)
- [SRE in 5 years: AI-first reliability (Rootly)](https://rootly.com/sre/sre-5-years-ai-first-reliability-autonomous-ops)

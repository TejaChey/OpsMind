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

**Evidence.** 15 new unit tests. All 4 governor rules were mutation-tested (each deliberately
broken rule fails a test). One real run on a real fault: Redis stopped, kill switch on,
`--live` → diagnosed correctly, governor refused, Redis stayed down, hand-off queued
(run 324f9bbf). **Not yet shown live:** the repeat limit and the failed-fix rule (unit tests
only), and the snapshot on a real live action.

## Still missing before a company would trust it (not built)

1. **Integrations**: real signal sources and a Kubernetes/cloud executor. The architecture
   (signal dict → tiers → mapped action → executor → verifier) would not change; the adapters
   would.
2. **Identity and permissions**: the executor uses the local Docker socket, i.e. full control.
   A real deployment needs a separate, least-privilege identity per action type.
3. **Change awareness**: don't act during a deploy, migration or maintenance window. Deploy
   facts are already read; a "freeze" check would sit in the governor.
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

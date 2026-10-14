---
type: Fault Type
title: Dependency down
description: A service the web app depends on (the database container) is not running;
  the app reports unhealthy but is only the symptom.
tags:
- fault
- dependency-down
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:03:27Z'
sources:
- id: run-c345bb38
  resource: ../trajectories/runs.jsonl#c345bb38-9185-4f1c-a4de-950b9cd0de27
  title: OpsMind run c345bb38-9185-4f1c-a4de-950b9cd0de27
  author: opsmind-supervisor/0.1
  last_modified: '2026-10-14T13:36:14.051631Z'
verified:
- by: process:opsmind-verify
  at: '2026-10-14T13:36:14.051631Z'
outcomes:
- run_id: c345bb38-9185-4f1c-a4de-950b9cd0de27
  at: '2026-10-14T13:36:14.051631Z'
  overridden: true
  proposed_fix: Restart the database container
  action: RESTART_DEPENDENCY
  verification_reason: healthy, SELECT 1 succeeded, app /health is 200 with db_ok
    true
  also_matched: []
---

A service the web app depends on (the database container) is not running; the app reports unhealthy but is only the symptom.

# Signals
* `container_state` is not 'running'
* `app_health_status` is not 200

# Code floor (cannot be overridden by the model)
app /health non-200 OR monitored container not running forces escalate=true and severity >= 0.8.

# Correct remediation (ground truth for evaluation)
RESTART_DEPENDENCY (start the container that is actually not running).

# Policy (from policy.yaml)
* `RESTART_DEPENDENCY`: auto-approve at confidence >= 0.7
* `RESTART_CONTAINER`: auto-approve at confidence >= 0.7

# Findings
* With facts about both containers the slow tier located the real cause (database exited, app only a symptom) in all runs so far.
* The executor starts only a container whose real state is not running; the target comes from code, not the model's words.

# Services

* [Postgres database](/services/postgres.md)
* [Web app](/services/web-app.md)

See the [runbook](/runbooks/dependency-down.md).

# Verified outcomes

| When (UTC) | Run | Fast tier | Slow tier proposed | Action | Result |
|---|---|---|---|---|---|
| 2026-10-14T13:36:14.051631Z | c345bb38[^run-c345bb38] | model said no; code floor overrode | Restart the database container | RESTART_DEPENDENCY | verified: healthy, SELECT 1 succeeded, app /health is 200 with db_ok true |

[^run-c345bb38]: Run c345bb38-9185-4f1c-a4de-950b9cd0de27 in trajectories/runs.jsonl

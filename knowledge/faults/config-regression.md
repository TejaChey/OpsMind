---
type: Fault Type
title: Config regression after deploy
description: A bad config deploy broke the web app while the database stayed healthy.
tags:
- fault
- config-regression
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:04:36Z'
sources:
- id: run-0a71189a
  resource: ../trajectories/runs.jsonl#0a71189a-8eae-4ef4-9824-29999ce85b1a
  title: OpsMind run 0a71189a-8eae-4ef4-9824-29999ce85b1a
  author: opsmind-supervisor/0.1
  last_modified: '2026-10-14T13:54:19.814142Z'
- id: run-a530b6a9
  resource: ../trajectories/runs.jsonl#a530b6a9-7192-41d8-9402-754f106d8b23
  title: OpsMind run a530b6a9-7192-41d8-9402-754f106d8b23
  author: opsmind-supervisor/0.1
  last_modified: '2026-10-14T14:04:36.300009Z'
verified:
- by: process:opsmind-verify
  at: '2026-10-14T13:54:19.814142Z'
- by: process:opsmind-verify
  at: '2026-10-14T14:04:36.300009Z'
outcomes:
- run_id: 0a71189a-8eae-4ef4-9824-29999ce85b1a
  at: '2026-10-14T13:54:19.814142Z'
  overridden: true
  proposed_fix: Roll back the web app's config to the last known-good deploy.
  action: ROLLBACK_CONFIG
  verification_reason: healthy, SELECT 1 succeeded, app /health is 200 with db_ok
    true; live config equals known-good deploy a5ffdbaf
  also_matched: []
- run_id: a530b6a9-7192-41d8-9402-754f106d8b23
  at: '2026-10-14T14:04:36.300009Z'
  overridden: true
  proposed_fix: Roll back the web app's config to the last known-good deploy.
  action: ROLLBACK_CONFIG
  verification_reason: healthy, SELECT 1 succeeded, app /health is 200 with db_ok
    true; live config equals known-good deploy 4087466b
  also_matched: []
---

A bad config deploy broke the web app while the database stayed healthy.

# Signals
* `app_health_status` is not 200 while the database is reachable
* `last_deploy` shows a recent deploy with status 'bad' and the changed settings

# Code floor (cannot be overridden by the model)
the app-unhealthy floor fires (app /health non-200).

# Correct remediation (ground truth for evaluation)
ROLLBACK_CONFIG (re-deploy the last known-good config).

# Policy (from policy.yaml)
* `ROLLBACK_CONFIG`: auto-approve at confidence >= 0.8

# Findings
* The slow tier diagnosed the changed port correctly, but without a rollback option on its menu proposed a network reconnect; with it, it proposed the rollback.
* A deploy counts as good only after 3 consecutive healthy probes; one early healthy reading was a false 'good'.

# Services

* [Web app](/services/web-app.md)
* [Postgres database](/services/postgres.md)

See the [runbook](/runbooks/config-regression.md).

# Verified outcomes

| When (UTC) | Run | Fast tier | Slow tier proposed | Action | Result |
|---|---|---|---|---|---|
| 2026-10-14T13:54:19.814142Z | 0a71189a[^run-0a71189a] | model said no; code floor overrode | Roll back the web app's config to the last known-good deploy. | ROLLBACK_CONFIG | verified: healthy, SELECT 1 succeeded, app /health is 200 with db_ok true; live config equals known-good deploy a5ffdbaf |
| 2026-10-14T14:04:36.300009Z | a530b6a9[^run-a530b6a9] | model said no; code floor overrode | Roll back the web app's config to the last known-good deploy. | ROLLBACK_CONFIG | verified: healthy, SELECT 1 succeeded, app /health is 200 with db_ok true; live config equals known-good deploy 4087466b |

[^run-0a71189a]: Run 0a71189a-8eae-4ef4-9824-29999ce85b1a in trajectories/runs.jsonl
[^run-a530b6a9]: Run a530b6a9-7192-41d8-9402-754f106d8b23 in trajectories/runs.jsonl

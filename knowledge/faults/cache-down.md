---
type: Fault Type
title: Cache down (cascading failure)
description: 'The Redis cache the web app also depends on is not running. The app
  reports unhealthy while the database is fine: the symptom is in one service, the
  cause in another, and a second possible cause (the database) has to be ruled out.'
tags:
- fault
- cache-down
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-11-06T15:00:23Z'
sources:
- id: run-679e1231
  resource: ../trajectories/runs.jsonl#679e1231-eefd-4678-82bf-e7cd5d82c742
  title: OpsMind run 679e1231-eefd-4678-82bf-e7cd5d82c742
  author: opsmind-supervisor/0.1
  last_modified: '2026-11-02T08:26:14.724552Z'
- id: run-7e268733
  resource: ../trajectories/runs.jsonl#7e268733-3b55-4d88-a555-c846c8074c37
  title: OpsMind run 7e268733-3b55-4d88-a555-c846c8074c37
  author: opsmind-supervisor/0.1
  last_modified: '2026-11-06T14:59:27.686477Z'
- id: run-3440bc20
  resource: ../trajectories/runs.jsonl#3440bc20-41ab-48a3-860c-3481f5876119
  title: OpsMind run 3440bc20-41ab-48a3-860c-3481f5876119
  author: opsmind-supervisor/0.1
  last_modified: '2026-11-06T15:00:23.851345Z'
verified:
- by: process:opsmind-verify
  at: '2026-11-02T08:26:14.724552Z'
- by: process:opsmind-verify
  at: '2026-11-06T14:59:27.686477Z'
- by: process:opsmind-verify
  at: '2026-11-06T15:00:23.851345Z'
outcomes:
- run_id: 679e1231-eefd-4678-82bf-e7cd5d82c742
  at: '2026-11-02T08:26:14.724552Z'
  overridden: true
  proposed_fix: Restart the Redis cache container
  action: RESTART_DEPENDENCY
  verification_reason: healthy, SELECT 1 succeeded, app /health is 200 with db_ok
    and redis_ok true
  also_matched: []
- run_id: 7e268733-3b55-4d88-a555-c846c8074c37
  at: '2026-11-06T14:59:27.686477Z'
  overridden: true
  proposed_fix: Restart the Redis cache container
  action: RESTART_DEPENDENCY
  verification_reason: healthy, SELECT 1 succeeded, app /health is 200 with db_ok
    and redis_ok true
  also_matched: []
- run_id: 3440bc20-41ab-48a3-860c-3481f5876119
  at: '2026-11-06T15:00:23.851345Z'
  overridden: true
  proposed_fix: Restart the Redis cache container
  action: RESTART_DEPENDENCY
  verification_reason: healthy, SELECT 1 succeeded, app /health is 200 with db_ok
    and redis_ok true
  also_matched: []
---

The Redis cache the web app also depends on is not running. The app reports unhealthy while the database is fine: the symptom is in one service, the cause in another, and a second possible cause (the database) has to be ruled out.

# Signals
* `redis_container_state` is not 'running'
* `app_health_status` is not 200 with `app_db_ok` true and `app_redis_ok` false

# Code floor (cannot be overridden by the model)
redis_container_state not running (and the app-unhealthy floor) forces escalate=true and severity >= 0.8.

# Correct remediation (ground truth for evaluation)
RESTART_DEPENDENCY (start the cache; the healthy database must NOT be restarted).

# Policy (from policy.yaml)
* `RESTART_DEPENDENCY`: auto-approve at confidence >= 0.7
* `RESTART_CONTAINER`: auto-approve at confidence >= 0.7

# Findings
* A generic 'restart the container' proposal would restart the monitored database, which is healthy; the mapper therefore sends 'restart the Redis/cache container' to RESTART_DEPENDENCY and a code guard blocks a generic restart when the cache is the one that is down.

# Services

* [Redis cache](/services/redis.md)
* [Web app](/services/web-app.md)
* [Postgres database](/services/postgres.md)

See the [runbook](/runbooks/cache-down.md).

# Verified outcomes

| When (UTC) | Run | Fast tier | Slow tier proposed | Action | Result |
|---|---|---|---|---|---|
| 2026-11-02T08:26:14.724552Z | 679e1231[^run-679e1231] | model said no; code floor overrode | Restart the Redis cache container | RESTART_DEPENDENCY | verified: healthy, SELECT 1 succeeded, app /health is 200 with db_ok and redis_ok true |
| 2026-11-06T14:59:27.686477Z | 7e268733[^run-7e268733] | model said no; code floor overrode | Restart the Redis cache container | RESTART_DEPENDENCY | verified: healthy, SELECT 1 succeeded, app /health is 200 with db_ok and redis_ok true |
| 2026-11-06T15:00:23.851345Z | 3440bc20[^run-3440bc20] | model said no; code floor overrode | Restart the Redis cache container | RESTART_DEPENDENCY | verified: healthy, SELECT 1 succeeded, app /health is 200 with db_ok and redis_ok true |

[^run-679e1231]: Run 679e1231-eefd-4678-82bf-e7cd5d82c742 in trajectories/runs.jsonl
[^run-7e268733]: Run 7e268733-3b55-4d88-a555-c846c8074c37 in trajectories/runs.jsonl
[^run-3440bc20]: Run 3440bc20-41ab-48a3-860c-3481f5876119 in trajectories/runs.jsonl

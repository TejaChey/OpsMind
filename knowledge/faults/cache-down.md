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
  at: '2026-11-02T08:26:14Z'
sources:
- id: run-679e1231
  resource: ../trajectories/runs.jsonl#679e1231-eefd-4678-82bf-e7cd5d82c742
  title: OpsMind run 679e1231-eefd-4678-82bf-e7cd5d82c742
  author: opsmind-supervisor/0.1
  last_modified: '2026-11-02T08:26:14.724552Z'
verified:
- by: process:opsmind-verify
  at: '2026-11-02T08:26:14.724552Z'
outcomes:
- run_id: 679e1231-eefd-4678-82bf-e7cd5d82c742
  at: '2026-11-02T08:26:14.724552Z'
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

[^run-679e1231]: Run 679e1231-eefd-4678-82bf-e7cd5d82c742 in trajectories/runs.jsonl

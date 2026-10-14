---
type: Fault Type
title: Out-of-memory kill
description: The kernel killed a process in the database container for exceeding its
  memory limit.
tags:
- fault
- oom
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:03:27Z'
outcomes: []
---

The kernel killed a process in the database container for exceeding its memory limit.

# Signals
* `oom_kill_delta` > 0 (cgroup `memory.events`, read before and after a window)

# Code floor (cannot be overridden by the model)
any `oom_kill_delta` > 0 forces escalate=true and severity >= 0.8.

# Correct remediation (ground truth for evaluation)
INCREASE_MEMORY_LIMIT (a human decides; capacity change).

# Policy (from policy.yaml)
* `INCREASE_MEMORY_LIMIT`: human approval required
* `RESTART_CONTAINER`: auto-approve at confidence >= 0.7

# Findings
* Docker's `OOMKilled` flag is sticky and the container can stay 'healthy' during a real incident, so the cgroup counter is the evidence.

# Services

* [Postgres database](/services/postgres.md)

See the [runbook](/runbooks/oom.md).

# Verified outcomes

No verified automated fix has been recorded for this fault yet.

---
type: Fault Type
title: CPU spike
description: The database container is pinned at its CPU limit and throttled by the
  kernel.
tags:
- fault
- cpu-spike
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:03:27Z'
outcomes: []
---

The database container is pinned at its CPU limit and throttled by the kernel.

# Signals
* `cpu_share_of_limit` >= 0.8
* `cpu_throttled_periods` > 0 (throttling is the trustworthy evidence)

# Code floor (cannot be overridden by the model)
share >= 0.8 AND throttled periods > 0 forces escalate=true and severity >= 0.8.

# Correct remediation (ground truth for evaluation)
TERMINATE_QUERY (end the runaway query; small and reversible).

# Policy (from policy.yaml)
* `TERMINATE_QUERY`: auto-approve at confidence >= 0.7
* `RESTART_CONTAINER`: auto-approve at confidence >= 0.7

# Findings
* The slow tier consistently proposed 'increase the CPU limit' instead of ending the query, even with full context and an action menu; that routes to UNKNOWN_NEEDS_HUMAN and is recorded as a finding, not forced.

# Services

* [Postgres database](/services/postgres.md)

See the [runbook](/runbooks/cpu-spike.md).

# Verified outcomes

No verified automated fix has been recorded for this fault yet.

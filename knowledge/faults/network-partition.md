---
type: Fault Type
title: Network partition
description: The database process is fine but the container is cut off from its network,
  so clients cannot connect.
tags:
- fault
- network-partition
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:03:27Z'
sources:
- id: run-62e6d114
  resource: ../trajectories/runs.jsonl#62e6d114-dd05-44ef-9d64-60d85fb59fc3
  title: OpsMind run 62e6d114-dd05-44ef-9d64-60d85fb59fc3
  author: opsmind-supervisor/0.1
  last_modified: '2026-10-14T13:22:22.652959Z'
verified:
- by: process:opsmind-verify
  at: '2026-10-14T13:22:22.652959Z'
outcomes:
- run_id: 62e6d114-dd05-44ef-9d64-60d85fb59fc3
  at: '2026-10-14T13:22:22.652959Z'
  overridden: true
  proposed_fix: Reconnect the container to its Docker network.
  action: RECONNECT_NETWORK
  verification_reason: healthy, SELECT 1 succeeded, 3 probes succeeded, max 0.007s
    < 1.0s
  also_matched: []
---

The database process is fine but the container is cut off from its network, so clients cannot connect.

# Signals
* `db_connect_ok` is false from the host
* `pg_ready_inside` is true (pg_isready run inside the container)

# Code floor (cannot be overridden by the model)
db_connect_ok False AND pg_ready_inside True forces escalate=true and severity >= 0.8.

# Correct remediation (ground truth for evaluation)
RECONNECT_NETWORK (re-attach the container, restoring its DNS aliases).

# Policy (from policy.yaml)
* `RECONNECT_NETWORK`: auto-approve at confidence >= 0.7
* `RESTART_CONTAINER`: auto-approve at confidence >= 0.7

# Findings
* Without a reconnect option in its menu the slow tier proposed 'Restart the container' (wrong); with it, it proposed the reconnect. The right proposal depended on the menu.
* Reconnecting without the compose DNS aliases left other services unable to find the database; verification now requires the alias.

# Services

* [Postgres database](/services/postgres.md)

See the [runbook](/runbooks/network-partition.md).

# Verified outcomes

| When (UTC) | Run | Fast tier | Slow tier proposed | Action | Result |
|---|---|---|---|---|---|
| 2026-10-14T13:22:22.652959Z | 62e6d114[^run-62e6d114] | model said no; code floor overrode | Reconnect the container to its Docker network. | RECONNECT_NETWORK | verified: healthy, SELECT 1 succeeded, 3 probes succeeded, max 0.007s < 1.0s |

[^run-62e6d114]: Run 62e6d114-dd05-44ef-9d64-60d85fb59fc3 in trajectories/runs.jsonl

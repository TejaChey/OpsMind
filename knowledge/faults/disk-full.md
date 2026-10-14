---
type: Fault Type
title: Disk full
description: The database's data volume is full and writes fail with 'No space left
  on device'.
tags:
- fault
- disk-full
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:03:27Z'
outcomes: []
---

The database's data volume is full and writes fail with 'No space left on device'.

# Signals
* `data_disk_percent_used` >= 0.95
* `write_failed` is true (a real 64 KB INSERT probe failed)

# Code floor (cannot be overridden by the model)
write_failed True OR usage >= 0.95 forces escalate=true and severity >= 0.8.

# Correct remediation (ground truth for evaluation)
INCREASE_DISK or FREE_DISK_SPACE (human only; deleting data is never automatic).

# Policy (from policy.yaml)
* `INCREASE_DISK`: human approval required
* `FREE_DISK_SPACE`: human approval required

# Findings
* A restart does not free the volume, so no safe automatable disk action exists; leaving it to a human is the intended result.
* The model's compound proposal 'increase the volume or remove data' maps to UNKNOWN_NEEDS_HUMAN.

# Services

* [Postgres database](/services/postgres.md)

See the [runbook](/runbooks/disk-full.md).

# Verified outcomes

No verified automated fix has been recorded for this fault yet.

---
type: Playbook
title: 'Runbook: CPU spike'
description: 'How OpsMind handles: cpu spike.'
tags:
- runbook
- cpu-spike
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T15:05:04Z'
---

# Trigger

The database container is pinned at its CPU limit and throttled by the kernel. See [CPU spike](/faults/cpu-spike.md).

# Steps

1. Sample CPU share and throttled periods.
2. Escalate (floor) and diagnose.
3. End the longest-running query (verified: CPU share falls below 0.5).

# Safety

The model never decides alone: escalation has a code floor, the action is chosen by rule-based mapping, policy.yaml gates execution, and success is decided by a deterministic check, never by the model.

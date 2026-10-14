---
type: Playbook
title: 'Runbook: Out-of-memory kill'
description: 'How OpsMind handles: out-of-memory kill.'
tags:
- runbook
- oom
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T15:05:04Z'
---

# Trigger

The kernel killed a process in the database container for exceeding its memory limit. See [Out-of-memory kill](/faults/oom.md).

# Steps

1. Read the cgroup `oom_kill` delta.
2. Escalate (floor) and diagnose.
3. Propose raising the memory limit; policy sends it to a human.

# Safety

The model never decides alone: escalation has a code floor, the action is chosen by rule-based mapping, policy.yaml gates execution, and success is decided by a deterministic check, never by the model.

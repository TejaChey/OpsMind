---
type: Playbook
title: 'Runbook: Disk full'
description: 'How OpsMind handles: disk full.'
tags:
- runbook
- disk-full
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T15:05:04Z'
---

# Trigger

The database's data volume is full and writes fail with 'No space left on device'. See [Disk full](/faults/disk-full.md).

# Steps

1. Read volume usage and run the write probe.
2. Escalate (floor) and diagnose.
3. Hand to a human; verification needs the write probe to succeed AND usage < 0.90.

# Safety

The model never decides alone: escalation has a code floor, the action is chosen by rule-based mapping, policy.yaml gates execution, and success is decided by a deterministic check, never by the model.

---
type: Playbook
title: 'Runbook: Dependency down'
description: 'How OpsMind handles: dependency down.'
tags:
- runbook
- dependency-down
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:04:36Z'
---

# Trigger

A service the web app depends on (the database container) is not running; the app reports unhealthy but is only the symptom. See [Dependency down](/faults/dependency-down.md).

# Steps

1. Check the app's /health and each container's state.
2. Escalate (floor) and diagnose.
3. Start the stopped dependency; verify DB healthy, SELECT 1, and app /health 200.

# Safety

The model never decides alone: escalation has a code floor, the action is chosen by rule-based mapping, policy.yaml gates execution, and success is decided by a deterministic check, never by the model.

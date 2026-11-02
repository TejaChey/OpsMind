---
type: Playbook
title: 'Runbook: Cache down (cascading failure)'
description: 'How OpsMind handles: cache down (cascading failure).'
tags:
- runbook
- cache-down
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-11-02T08:26:14Z'
---

# Trigger

The Redis cache the web app also depends on is not running. The app reports unhealthy while the database is fine: the symptom is in one service, the cause in another, and a second possible cause (the database) has to be ruled out. See [Cache down (cascading failure)](/faults/cache-down.md).

# Steps

1. Check the app's /health (which dependency does it blame?) and each container's state.
2. Escalate (floor) and diagnose.
3. Start only the stopped container; verify the app reports db_ok AND redis_ok true.

# Safety

The model never decides alone: escalation has a code floor, the action is chosen by rule-based mapping, policy.yaml gates execution, and success is decided by a deterministic check, never by the model.

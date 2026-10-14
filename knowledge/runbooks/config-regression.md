---
type: Playbook
title: 'Runbook: Config regression after deploy'
description: 'How OpsMind handles: config regression after deploy.'
tags:
- runbook
- config-regression
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:04:36Z'
---

# Trigger

A bad config deploy broke the web app while the database stayed healthy. See [Config regression after deploy](/faults/config-regression.md).

# Steps

1. Read the app's health and the newest deploy with its config diff.
2. Escalate (floor) and diagnose.
3. Roll back to the newest good deploy; verify app /health 200 AND live config equals it.

# Safety

The model never decides alone: escalation has a code floor, the action is chosen by rule-based mapping, policy.yaml gates execution, and success is decided by a deterministic check, never by the model.

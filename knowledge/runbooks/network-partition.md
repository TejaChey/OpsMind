---
type: Playbook
title: 'Runbook: Network partition'
description: 'How OpsMind handles: network partition.'
tags:
- runbook
- network-partition
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:04:36Z'
---

# Trigger

The database process is fine but the container is cut off from its network, so clients cannot connect. See [Network partition](/faults/network-partition.md).

# Steps

1. Probe the connection from the host and pg_isready inside.
2. Escalate (floor) and diagnose.
3. Reconnect to the compose network with aliases; verify 3 fast probes plus the DNS alias.

# Safety

The model never decides alone: escalation has a code floor, the action is chosen by rule-based mapping, policy.yaml gates execution, and success is decided by a deterministic check, never by the model.

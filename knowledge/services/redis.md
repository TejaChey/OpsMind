---
type: Service
title: Redis cache
description: The cache container (opsmind-redis) the web app pings in its /health,
  added in Phase 1 so one service's symptom can come from another service's failure.
tags:
- service
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-11-02T08:26:14Z'
---

The cache container (opsmind-redis) the web app pings in its /health, added in Phase 1 so one service's symptom can come from another service's failure.

# Faults seen on this service

* [Cache down (cascading failure)](/faults/cache-down.md)

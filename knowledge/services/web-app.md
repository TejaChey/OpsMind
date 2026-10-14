---
type: Service
title: Web app
description: A small Flask service (opsmind-app) whose /health does a real database
  round trip; its config is deployed from a file.
tags:
- service
generated:
  by: opsmind-knowledge-writer/0.1
  at: '2026-10-14T14:04:36Z'
---

A small Flask service (opsmind-app) whose /health does a real database round trip; its config is deployed from a file.

# Faults seen on this service

* [Dependency down](/faults/dependency-down.md)
* [Config regression after deploy](/faults/config-regression.md)

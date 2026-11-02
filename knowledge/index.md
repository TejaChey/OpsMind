---
okf_version: "0.2"
---

# Services

* [Postgres database](services/postgres.md) - The target database container (opsmind-postgres): 256 MB memory, 0.5 CPU, 96 MB data volume.
* [Redis cache](services/redis.md) - The cache container (opsmind-redis) the web app pings in its /health, added in Phase 1 so one service's symptom can come from another service's failure.
* [Web app](services/web-app.md) - A small Flask service (opsmind-app) whose /health does a real database round trip; its config is deployed from a file.

# Fault types

* [Cache down (cascading failure)](faults/cache-down.md) - The Redis cache the web app also depends on is not running. The app reports unhealthy while the database is fine: the symptom is in one service, the cause in another, and a second possible cause (the database) has to be ruled out.
* [Config regression after deploy](faults/config-regression.md) - A bad config deploy broke the web app while the database stayed healthy.
* [CPU spike](faults/cpu-spike.md) - The database container is pinned at its CPU limit and throttled by the kernel.
* [Dependency down](faults/dependency-down.md) - A service the web app depends on (the database container) is not running; the app reports unhealthy but is only the symptom.
* [Disk full](faults/disk-full.md) - The database's data volume is full and writes fail with 'No space left on device'.
* [Network partition](faults/network-partition.md) - The database process is fine but the container is cut off from its network, so clients cannot connect.
* [Out-of-memory kill](faults/oom.md) - The kernel killed a process in the database container for exceeding its memory limit.

# Runbooks

* [Runbook: Cache down (cascading failure)](runbooks/cache-down.md) - How OpsMind handles: cache down (cascading failure).
* [Runbook: Config regression after deploy](runbooks/config-regression.md) - How OpsMind handles: config regression after deploy.
* [Runbook: CPU spike](runbooks/cpu-spike.md) - How OpsMind handles: cpu spike.
* [Runbook: Dependency down](runbooks/dependency-down.md) - How OpsMind handles: dependency down.
* [Runbook: Disk full](runbooks/disk-full.md) - How OpsMind handles: disk full.
* [Runbook: Network partition](runbooks/network-partition.md) - How OpsMind handles: network partition.
* [Runbook: Out-of-memory kill](runbooks/oom.md) - How OpsMind handles: out-of-memory kill.

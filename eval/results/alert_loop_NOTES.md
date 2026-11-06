# OpsMind behind standard monitoring (2026-11-06)

**Question:** can OpsMind run behind the alerting a company already has, and do the
cross-incident limits hold on a real, repeating fault?

## Setup
- `docker compose -f services/docker-compose.yml --profile monitoring up -d`: Prometheus
  v2.53.0 scrapes the app's new `/metrics` (0/1 gauges from the same real DB + Redis checks as
  `/health`) every 5 s; alert `OpsMindAppUnhealthy` fires after 10 s of `opsmind_app_healthy == 0`;
  Alertmanager v0.27.0 posts it (Bearer token) to `tools/alert_receiver.py` on the host.
- The receiver treats the alert only as a trigger: it runs `agents/supervisor.py`, which
  re-measures everything itself. Alert text never reaches a model (unit-tested with an
  injection string in the annotations).
- Fault: `faults/dependency_down.py --dependency opsmind-redis --hold` (real `docker stop`).
- Ledger: `trajectories/alerts.jsonl`; table below from `python tools/alert_receiver.py --report`.

## Results (n = 1 dry run + 3 live rounds, back to back)

| Round | Mode | OpsMind did | Verified | Alert open (Alertmanager startsAt→endsAt) | Run |
|---|---|---|---|---|---|
| 0 | dry run | would RESTART_DEPENDENCY (Redis) | – | 0.58 min (cleared by hand) | ddbd4294 |
| 1 | live | RESTART_DEPENDENCY executed | yes | 0.42 min | 7e268733 |
| 2 | live | RESTART_DEPENDENCY executed | yes | 0.42 min | 3440bc20 |
| 3 | live | **refused by governor** (2 identical fixes in 30 min = not lasting) | – | 0.50 min (**cleared by hand**) | cec11838 |

- Rounds 1 and 2: alert → triage → fix → deterministic verification → alert resolved, with no
  human. Each fix got a pre-action snapshot (container states + 50 Redis log lines) and was
  recorded in the knowledge bundle (`knowledge/faults/cache-down.md`, still conformant).
- Round 3: the same diagnosis and proposed fix, but the governor refused and the run was queued
  in `trajectories/escalations.jsonl` with the reason. Redis stayed down until restarted by hand.
  This is the first time the repeat limit has been exercised on a real fault (until now it was
  unit tests only).
- Every alert produced exactly one triage (Alertmanager repeats were deduplicated by
  fingerprint + startsAt), and every "resolved" notification was linked back to its run.

## Limits
- n = 3 live rounds, one fault type (cache down), one machine. Timings include Prometheus's
  `for: 10s` and 5 s scrape interval, so "alert open" is not OpsMind's own speed.
- Round 0's and round 3's resolution times are mine (manual clear), not OpsMind's.
- Recreating the stack for the monitoring profile restarted Postgres (tmpfs data volume:
  test data only).
- The receiver runs one triage at a time by design; throughput under many concurrent alerts
  was not tested.

# Verification on a fix that did not fix anything (2026-10-15)

Script `eval/verification_wrong_fix.py`, data `verification_wrong_fix.jsonl`. Each real fault is held
(the state a wrong or no-op fix leaves behind; no fix is applied, so no new live action is taken),
confirmed present by the independent check in `eval/ground_truth.py`, then two verifiers run on it:
plain (container healthy + SELECT 1) and fault-specific (what the supervisor really uses).
After cleanup both run again as a control.

| fault | fault present | plain check | fault-specific check | control (fixed): plain / specific |
|---|---|---|---|---|
| network partition | yes | rejected | rejected | pass / pass |
| config regression | yes | **false pass** | rejected (app /health 503, db_ok false) | pass / pass |
| disk full | yes | **false pass** | rejected (write probe: No space left on device) | pass / pass |
| dependency down | yes | rejected | rejected | pass / pass |

Plain check false passes 2/4; fault-specific check false passes 0/4; the specific check passes
after cleanup 4/4, so it does not simply fail everything.

## What this does and does not show
* The benefit is real but fault-dependent: the plain check is fooled when the database itself is
  fine and the fault lives elsewhere (config: the web app; disk: writes, not reads).
* The plain check was NOT fooled for network or dependency faults here, because it runs a real
  SELECT 1 from the host, which fails during a partition and when the container is down. An
  earlier held-fault finding (reconnect without the DNS alias) is a different state and is covered
  by the network check's alias requirement, not by this table.
* n = 1 per fault, one machine. It is a demonstration on confirmed-present faults, not a rate.
* No wrong fix was actually executed; a held fault stands in for one. Executing restart-style
  actions as a deliberately wrong fix would need explicit live approval.

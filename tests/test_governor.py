"""Cross-incident limits (tools/governor.py), the human hand-off queue (tools/escalation.py)
and how supervisor.run_once uses them. No Docker or Ollama needed."""
import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))
from tools import escalation, governor  # noqa: E402

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
CFG = {"automation_enabled": True, "window_minutes": 30, "max_executions_per_target": 2}
NO_PAUSE = "/nonexistent/OPSMIND_PAUSED"


def run(minutes_ago, action="RESTART_CONTAINER", container="opsmind-postgres", verified=True,
        dry_run=False, status="executed", started=None, **extra):
    ex = {"action": action, "container": container, "dry_run": dry_run, "status": status}
    if started:
        ex["started"] = started
    return {"run_id": f"run-{minutes_ago}-{action}", "execution": ex,
            "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "verification": {"verified": verified}, **extra}


def check(action, targets, history, **kw):
    return governor.check(action, targets, history=history, now=NOW, config=kw.get("cfg", CFG),
                          pause_file=kw.get("pause", NO_PAUSE),
                          deploy_lock=kw.get("lock", "/nonexistent/IN_PROGRESS"))


class Governor(unittest.TestCase):
    def test_no_history_allows(self):
        self.assertTrue(check("RESTART_CONTAINER", ["opsmind-postgres"], [])[0])

    def test_failed_verification_in_window_blocks_a_repeat(self):
        ok, why, ev = check("RESTART_CONTAINER", ["opsmind-postgres"], [run(5, verified=False)])
        self.assertFalse(ok)
        self.assertIn("did not verify", why)
        self.assertEqual(ev["recent_run_ids"], ["run-5-RESTART_CONTAINER"])

    def test_limit_reached_blocks_even_when_every_fix_verified(self):
        hist = [run(5), run(20)]
        ok, why, _ = check("RESTART_CONTAINER", ["opsmind-postgres"], hist)
        self.assertFalse(ok)
        self.assertIn("not lasting", why)
        self.assertTrue(check("RESTART_CONTAINER", ["opsmind-postgres"], hist[:1])[0])

    def test_old_runs_outside_window_do_not_count(self):
        hist = [run(31, verified=False), run(45), run(60)]
        self.assertTrue(check("RESTART_CONTAINER", ["opsmind-postgres"], hist)[0])

    def test_dry_runs_harness_and_unexecuted_do_not_count(self):
        hist = [run(1, dry_run=True, verified=False), run(2, status="would_execute"),
                run(3, verified=False, HARNESS_TEST=True), run(4, status="blocked_by_governor")]
        self.assertTrue(check("RESTART_CONTAINER", ["opsmind-postgres"], hist)[0])

    def test_other_action_or_other_target_does_not_count(self):
        hist = [run(1, action="ROLLBACK_CONFIG", verified=False),
                run(2, container="other", verified=False)]
        self.assertTrue(check("RESTART_CONTAINER", ["opsmind-postgres"], hist)[0])

    def test_dependency_restarts_are_keyed_by_the_dependency_started(self):
        hist = [run(3, action="RESTART_DEPENDENCY", started={"opsmind-redis": {}}),
                run(9, action="RESTART_DEPENDENCY", started={"opsmind-redis": {}})]
        self.assertFalse(check("RESTART_DEPENDENCY", ["opsmind-redis"], hist)[0])
        self.assertTrue(check("RESTART_DEPENDENCY", ["opsmind-postgres"], hist)[0])

    def test_pause_file_and_config_switch_stop_everything(self):
        with tempfile.TemporaryDirectory() as d:
            pause = Path(d) / "OPSMIND_PAUSED"
            pause.touch()
            ok, why, _ = check("RESTART_CONTAINER", ["x"], [], pause=pause)
            self.assertFalse(ok)
            self.assertIn("paused", why)
        ok, why, _ = check("RESTART_CONTAINER", ["x"], [], cfg={**CFG, "automation_enabled": False})
        self.assertFalse(ok)

    def test_deploy_in_progress_freezes_automation(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "IN_PROGRESS"
            lock.write_text('{"label": "v2"}')
            ok, why, ev = check("ROLLBACK_CONFIG", ["opsmind-postgres"], [], lock=lock)
            self.assertFalse(ok)
            self.assertIn("change freeze", why)
            self.assertTrue(ev["frozen"])

    def test_deploy_holds_the_lock_only_while_health_is_undecided(self):
        from tools import deploy
        with tempfile.TemporaryDirectory() as d:
            live, hist = Path(d) / "live" / "app.json", Path(d) / "history.jsonl"
            lock = Path(d) / "IN_PROGRESS"
            seen = []
            def probe(*a, **k):
                seen.append(lock.exists())
                return True, "HTTP 200 x3 in a row"
            with mock.patch.object(deploy, "wait_healthy", side_effect=probe):
                deploy.deploy({"db_port": 1}, "t", 5050, history_path=hist, live_path=live)
            self.assertEqual(seen, [True])
            self.assertFalse(lock.exists())
            with mock.patch.object(deploy, "wait_healthy", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    deploy.deploy({"db_port": 1}, "t", 5050, history_path=hist, live_path=live)
            self.assertFalse(lock.exists())     # removed even when the deploy crashes

    def test_unreadable_history_fails_closed(self):
        with mock.patch.object(governor, "load_history", side_effect=ValueError("bad json")):
            ok, why, _ = governor.check("RESTART_CONTAINER", ["x"], now=NOW, config=CFG,
                                        pause_file=NO_PAUSE, deploy_lock="/nonexistent/L")
        self.assertFalse(ok)
        self.assertIn("failing closed", why)

    def test_policy_yaml_has_a_governor_section(self):
        cfg = governor.load_config()
        self.assertIs(cfg["automation_enabled"], True)
        self.assertGreaterEqual(cfg["max_executions_per_target"], 1)


class Escalation(unittest.TestCase):
    def rec(self, status, verified=None):
        return {"run_id": "r1", "execution": {"status": status, "reason": "x"},
                "verification": None if verified is None else {"verified": verified}}

    def test_who_needs_a_human(self):
        self.assertIsNone(escalation.needs_human({"run_id": "r", "execution": None}))
        self.assertIsNone(escalation.needs_human(self.rec("executed", True)))
        self.assertIsNone(escalation.needs_human(self.rec("would_execute")))
        for s in ("blocked_by_governor", "blocked_pending_human_approval", "failed",
                  "not_implemented", "no_target"):
            self.assertIsNotNone(escalation.needs_human(self.rec(s)), s)
        self.assertIn("did not pass", escalation.needs_human(self.rec("executed", False)))
        self.assertIsNotNone(escalation.needs_human(self.rec("executed")))   # unverified

    def test_escalate_writes_only_when_needed(self):
        with tempfile.TemporaryDirectory() as d:
            q = Path(d) / "q.jsonl"
            self.assertIsNone(escalation.escalate(self.rec("executed", True), q))
            self.assertFalse(q.exists())
            entry = escalation.escalate(self.rec("failed"), q)
            self.assertEqual(entry["trajectory"], "trajectories/runs.jsonl#r1")
            self.assertEqual(len(q.read_text().splitlines()), 1)


class SupervisorWiring(unittest.TestCase):
    """run_once with a governor: the plan is a dry run, the governor sees the real targets, a
    refusal never reaches a live executor call, and an allowed action gets a snapshot."""

    def setUp(self):
        import supervisor
        self.sup = supervisor
        fast = {"decision": {"escalate": True, "severity": 0.9}, "signal": {}, "model": "m",
                "model_raw": "", "overridden": False, "source": "model", "attempts": 1}
        slow = {"source": "model", "diagnosis": {"root_cause": "redis exited",
                                                 "proposed_fix": "Restart the cache container",
                                                 "confidence": 0.9}}
        self.calls = []

        def fake_execute(action, conf, container, dry_run, **kw):
            self.calls.append(dry_run)
            return {"action": action, "container": container, "dry_run": dry_run,
                    "status": "would_execute" if dry_run else "executed", "reason": "",
                    "targets": ["opsmind-redis"]}
        self.patches = [mock.patch.object(supervisor.fast_tier, "classify", return_value=fast),
                        mock.patch.object(supervisor.slow_tier, "diagnose", return_value=slow),
                        mock.patch.object(supervisor.executor, "execute", side_effect=fake_execute),
                        mock.patch.object(supervisor.health_check, "verify",
                                          return_value={"verified": True, "elapsed_seconds": 1,
                                                        "reason": ""}),
                        mock.patch.object(supervisor.forensics, "snapshot",
                                          return_value={"opsmind-redis": {"state": "exited"}}),
                        mock.patch.object(supervisor, "write_trajectory")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def go(self, verdict, dry_run=False):
        seen = []
        def gov(action, targets):
            seen.append((action, list(targets)))
            return verdict, "why", {}
        rec = self.sup.run_once({}, dry_run=dry_run, db={}, governor_check=gov)
        return rec, seen

    def test_refusal_never_executes_live(self):
        rec, seen = self.go(False)
        self.assertEqual(seen, [("RESTART_DEPENDENCY", ["opsmind-redis"])])
        self.assertEqual(self.calls, [True])            # only the plan, no live call
        self.assertEqual(rec["execution"]["status"], "blocked_by_governor")
        self.assertIsNone(rec["verification"])
        self.assertIsNone(rec["pre_action_snapshot"])

    def test_allowed_snapshots_then_executes(self):
        rec, _ = self.go(True)
        self.assertEqual(self.calls, [True, False])
        self.assertEqual(rec["execution"]["status"], "executed")
        self.assertIn("opsmind-redis", rec["pre_action_snapshot"])
        self.assertTrue(rec["governor"]["allowed"])

    def test_investigator_mode_uses_the_same_gates(self):
        slow = {"source": "model", "trace": [{"tool": "list_containers"}],
                "diagnosis": {"root_cause": "redis exited", "proposed_fix": "Restart the cache container",
                              "confidence": 0.9}}
        with mock.patch.object(self.sup.investigator, "investigate", return_value=slow) as inv:
            rec = self.sup.run_once({}, dry_run=False, db={}, toolbox=object(),
                                    governor_check=lambda a, t: (False, "no", {}))
        inv.assert_called_once()
        self.assertEqual(rec["execution"]["status"], "blocked_by_governor")   # same governor
        self.assertEqual(rec["slow_tier"]["trace"], [{"tool": "list_containers"}])

    def cross(self, inv_fix, inv_conf=0.75):
        inv = {"source": "model", "trace": [],
               "diagnosis": {"root_cause": "x", "proposed_fix": inv_fix, "confidence": inv_conf}}
        with mock.patch.object(self.sup.investigator, "investigate", return_value=inv):
            return self.sup.run_once({}, dry_run=True, db={}, toolbox=object(), cross_check=True)

    def test_cross_check_agreement_acts_with_the_lower_confidence(self):
        rec = self.cross("Start the redis cache container again", 0.75)
        self.assertEqual(rec["mapped_action"], "RESTART_DEPENDENCY")
        self.assertTrue(rec["slow_tier"]["cross_check"]["agree"])
        self.assertEqual(rec["execution"]["status"], "would_execute")

    def test_cross_check_disagreement_goes_to_a_human(self):
        rec = self.cross("Reconnect the container to its Docker network")
        self.assertEqual(rec["mapped_action"], "UNKNOWN_NEEDS_HUMAN")
        self.assertFalse(rec["slow_tier"]["cross_check"]["agree"])
        self.assertEqual(rec["execution"]["action"], "UNKNOWN_NEEDS_HUMAN")
        from tools import executor             # and the REAL policy never auto-runs that
        self.assertFalse(executor.check_policy("UNKNOWN_NEEDS_HUMAN", 1.0, executor.load_policy())[0])

    def test_dry_run_does_not_consult_the_governor(self):
        rec, seen = self.go(False, dry_run=True)
        self.assertEqual(seen, [])
        self.assertEqual(rec["execution"]["status"], "would_execute")


if __name__ == "__main__":
    unittest.main()

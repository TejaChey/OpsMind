"""Tests for the evaluation runner's safety guard and ground-truth plumbing (no Docker needed).

The guard matters most: the evaluation must never run a live action that the user has not
approved live, and must never run live on faults whose fix is human-only.

Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "agents", ROOT / "eval"):
    sys.path.insert(0, str(p))

import run_eval
from tools import executor


class GuardTests(unittest.TestCase):
    def run_guarded(self, live_allowed, action, requested_dry_run):
        seen = {}

        def fake(action, confidence, container, dry_run=True, **kw):
            seen["dry_run"] = dry_run
            return {}

        with mock.patch.object(executor, "execute", fake):
            restore = run_eval.guard_executor(live_allowed)
            try:
                executor.execute(action, 0.9, "c", dry_run=requested_dry_run)
            finally:
                restore()
        return seen["dry_run"]

    def test_approved_action_on_live_fault_may_run_live(self):
        for a in ("RECONNECT_NETWORK", "RESTART_DEPENDENCY", "ROLLBACK_CONFIG"):
            self.assertFalse(self.run_guarded(True, a, False), a)

    def test_unapproved_live_actions_are_forced_dry(self):
        for a in ("RESTART_CONTAINER", "TERMINATE_QUERY", "INCREASE_MEMORY_LIMIT",
                  "FREE_DISK_SPACE", "UNKNOWN_NEEDS_HUMAN"):
            self.assertTrue(self.run_guarded(True, a, False), a)

    def test_human_only_faults_are_always_dry(self):
        self.assertTrue(self.run_guarded(False, "ROLLBACK_CONFIG", False))

    def test_dry_run_request_is_never_upgraded_to_live(self):
        self.assertTrue(self.run_guarded(True, "ROLLBACK_CONFIG", True))

    def test_guard_restores_the_real_executor(self):
        before = executor.execute
        run_eval.guard_executor(True)()
        self.assertIs(executor.execute, before)

    def test_live_set_is_exactly_the_user_approved_kinds(self):
        self.assertEqual(run_eval.APPROVED_LIVE,
                         {"RECONNECT_NETWORK", "RESTART_DEPENDENCY", "ROLLBACK_CONFIG"})

    def test_every_planned_fault_has_a_ground_truth_action(self):
        for f in run_eval.PLAN:
            self.assertIn(f, run_eval.GROUND_TRUTH)


class EvalDoesNotTouchRealLogs(unittest.TestCase):
    def test_output_paths_are_separate_from_real_logs(self):
        self.assertEqual(run_eval.OUT.parent.name, "results")
        self.assertNotEqual(run_eval.OUT.name, "runs.jsonl")
        src = Path(run_eval.__file__).read_text()
        self.assertNotIn("record_incident", src)   # never writes to the knowledge bundle


if __name__ == "__main__":
    unittest.main()

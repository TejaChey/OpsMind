"""Tests for ROLLBACK_CONFIG: mapper wording, policy gate, executor guards, and the
"live config must equal the known-good config" verification.

Deploy history/files are stubbed, so no Docker is needed.
Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT))

from action_mapper import Action, map_action
from tools import deploy, executor
from verify import health_check as hc

GOOD = {"db_port": 5432}
BAD = {"db_port": 5439}
TARGET = {"deploy_id": "g1", "config": GOOD, "status": "good"}


class Mapper(unittest.TestCase):
    def test_rollback_wordings(self):
        for t in ("Roll back the web app's config to the last known-good deploy",
                  "Revert the database port change", "Restore the previous configuration",
                  "Rollback the deployment"):
            self.assertEqual(map_action(t), Action.ROLLBACK_CONFIG, t)

    def test_rollback_plus_restart_is_a_conflict(self):
        self.assertEqual(map_action("Roll back the config and restart the database container"),
                         Action.UNKNOWN_NEEDS_HUMAN)

    def test_negated_rollback_is_unknown(self):
        self.assertEqual(map_action("Do not roll back the config"), Action.UNKNOWN_NEEDS_HUMAN)

    def test_unrelated_text_unchanged(self):
        self.assertEqual(map_action("Restart the service"), Action.RESTART_CONTAINER)


class Policy(unittest.TestCase):
    def test_needs_high_confidence(self):
        pol = executor.load_policy()
        self.assertTrue(executor.check_policy("ROLLBACK_CONFIG", 0.8, pol)[0])
        self.assertFalse(executor.check_policy("ROLLBACK_CONFIG", 0.79, pol)[0])


class ExecutorGuards(unittest.TestCase):
    def test_no_target_means_nothing_happens(self):
        with mock.patch.object(deploy, "rollback_target", return_value=(None, "already equal")), \
             mock.patch.object(deploy, "deploy") as dep:
            status, _, _ = executor.rollback_config(5050, dry_run=False)
        self.assertEqual(status, "no_target")
        dep.assert_not_called()

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(deploy, "rollback_target", return_value=(TARGET, "differs")), \
             mock.patch.object(deploy, "read_live", return_value=BAD), \
             mock.patch.object(deploy, "deploy") as dep:
            status, _, ev = executor.rollback_config(5050, dry_run=True)
        self.assertEqual(status, "would_execute")
        self.assertEqual(ev["config_diff"], {"db_port": {"from": 5439, "to": 5432}})
        dep.assert_not_called()

    def run_live(self, rollback_status):
        entry = {"deploy_id": "r1", "status": rollback_status, "health_after": "HTTP 503"}
        with mock.patch.object(deploy, "rollback_target", return_value=(TARGET, "differs")), \
             mock.patch.object(deploy, "read_live", return_value=BAD), \
             mock.patch.object(deploy, "deploy", return_value=entry) as dep:
            return executor.rollback_config(5050, dry_run=False), dep

    def test_live_rolls_back_to_the_known_good_config(self):
        (status, _, ev), dep = self.run_live("good")
        self.assertEqual(status, "executed")
        self.assertEqual(dep.call_args.args[0], GOOD)
        self.assertEqual(dep.call_args.kwargs["restores"], "g1")

    def test_written_but_unhealthy_is_a_failure_not_a_success(self):
        (status, _, _), _ = self.run_live("bad")
        self.assertEqual(status, "failed")

    def test_live_without_app_port_fails_closed(self):
        with mock.patch.object(deploy, "rollback_target", return_value=(TARGET, "differs")), \
             mock.patch.object(deploy, "read_live", return_value=BAD), \
             mock.patch.object(deploy, "deploy") as dep:
            status, _, _ = executor.rollback_config(None, dry_run=False)
        self.assertEqual(status, "failed")
        dep.assert_not_called()


class ConfigVerification(unittest.TestCase):
    def check(self, history, live):
        with mock.patch.object(deploy, "read_history", return_value=history), \
             mock.patch.object(deploy, "read_live", return_value=live):
            return hc._config_matches_known_good()[0]

    def test_matches(self):
        self.assertTrue(self.check([TARGET], GOOD))

    def test_differs_fails(self):
        self.assertFalse(self.check([TARGET], BAD))

    def test_no_known_good_fails_closed(self):
        self.assertFalse(self.check([{"deploy_id": "b", "config": BAD, "status": "bad"}], BAD))

    def test_unreadable_live_config_fails(self):
        self.assertFalse(self.check([TARGET], None))


if __name__ == "__main__":
    unittest.main()

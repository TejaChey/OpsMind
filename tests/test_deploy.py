"""Tests for tools/deploy.py: history, last-known-good, diffs, rollback target, and the
"one early healthy reading is not a good deploy" rule. Uses temp files; no Docker needed.

Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import deploy

GOOD = {"db_host": "postgres", "db_port": 5432}
BAD = {"db_host": "postgres", "db_port": 5439}


class DeployTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.hist = Path(d.name) / "history.jsonl"
        self.live = Path(d.name) / "live" / "app.json"

    def entry(self, i, status, config, ts=None, kind="deploy"):
        e = {"deploy_id": i, "timestamp": (ts or datetime.now(timezone.utc)).isoformat(),
             "kind": kind, "label": i, "config": config, "status": status}
        deploy.append_history(e, self.hist)
        return e

    def test_last_good_skips_bad_entries(self):
        self.entry("a", "good", GOOD)
        self.entry("b", "bad", BAD)
        self.assertEqual(deploy.last_good(deploy.read_history(self.hist))["deploy_id"], "a")

    def test_last_good_none_when_all_bad_or_empty(self):
        self.assertIsNone(deploy.last_good([]))
        self.entry("b", "bad", BAD)
        self.assertIsNone(deploy.last_good(deploy.read_history(self.hist)))

    def test_diff_shows_only_changed_keys(self):
        self.assertEqual(deploy.diff(GOOD, BAD), {"db_port": {"from": 5432, "to": 5439}})
        self.assertEqual(deploy.diff(GOOD, GOOD), {})

    def test_rollback_target_when_live_differs(self):
        self.entry("a", "good", GOOD)
        self.entry("b", "bad", BAD)
        deploy.write_live(BAD, self.live)
        target, _ = deploy.rollback_target(self.hist, self.live)
        self.assertEqual(target["deploy_id"], "a")

    def test_no_rollback_target_when_already_on_good_config(self):
        self.entry("a", "good", GOOD)
        deploy.write_live(GOOD, self.live)
        self.assertIsNone(deploy.rollback_target(self.hist, self.live)[0])

    def test_no_rollback_target_without_known_good(self):
        self.entry("b", "bad", BAD)
        deploy.write_live(BAD, self.live)
        target, why = deploy.rollback_target(self.hist, self.live)
        self.assertIsNone(target)
        self.assertIn("no known-good", why)

    def test_recent_deploy_facts_has_age_and_diff(self):
        now = datetime.now(timezone.utc)
        self.entry("a", "good", GOOD, ts=now - timedelta(minutes=10))
        self.entry("b", "bad", BAD, ts=now - timedelta(seconds=30))
        f = deploy.recent_deploy_facts(self.hist, self.live, now=now)
        self.assertEqual(f["deploy_id"], "b")
        self.assertEqual(f["age_seconds"], 30.0)
        self.assertEqual(f["status_when_deployed"], "bad")
        self.assertEqual(f["changed_vs_last_known_good"], {"db_port": {"from": 5432, "to": 5439}})

    def test_recent_deploy_facts_none_without_history(self):
        self.assertIsNone(deploy.recent_deploy_facts(self.hist, self.live))

    def test_write_live_roundtrip(self):
        deploy.write_live(GOOD, self.live)
        self.assertEqual(deploy.read_live(self.live), GOOD)


class StableHealth(unittest.TestCase):
    def run_probes(self, results, seconds=100):
        with mock.patch.object(deploy, "app_is_healthy", side_effect=results), \
             mock.patch.object(deploy.time, "sleep"):
            return deploy.wait_healthy(5050, seconds=seconds)

    def test_one_early_healthy_reading_then_failure_is_not_good(self):
        # The real bug: the app still ran on the OLD config for one probe, then broke.
        ok, _ = self.run_probes([(True, "x"), (False, "HTTP 503"), (False, "HTTP 503")],
                                seconds=0)
        self.assertFalse(ok)

    def test_streak_resets_after_a_failure(self):
        probes = [(True, "x"), (True, "x"), (False, "HTTP 503"), (True, "x"), (True, "x"),
                  (True, "x")]
        self.assertTrue(self.run_probes(probes)[0])

    def test_three_in_a_row_is_good(self):
        self.assertTrue(self.run_probes([(True, "x")] * 3)[0])

    def test_two_healthy_is_not_enough(self):
        with mock.patch.object(deploy, "app_is_healthy", side_effect=[(True, "x")] * 2), \
             mock.patch.object(deploy.time, "monotonic", side_effect=[0, 1, 99]), \
             mock.patch.object(deploy.time, "sleep"):
            self.assertFalse(deploy.wait_healthy(5050, seconds=5)[0])


if __name__ == "__main__":
    unittest.main()

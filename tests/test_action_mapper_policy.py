"""Tests for the safety properties of the action mapper and policy.yaml.

Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT))

from action_mapper import Action, map_action
from tools import executor


class MapperTests(unittest.TestCase):
    def test_existing_actions_unchanged(self):
        cases = {
            "Increase the memory limit for the container.": Action.INCREASE_MEMORY_LIMIT,
            "Restart the service": Action.RESTART_CONTAINER,
            "Terminate the longest-running query": Action.TERMINATE_QUERY,
            "No action needed": Action.NO_ACTION,
        }
        for text, want in cases.items():
            self.assertEqual(map_action(text), want, text)

    def test_disk_actions(self):
        self.assertEqual(map_action("Increase the size of the data volume"), Action.INCREASE_DISK)
        self.assertEqual(map_action("Delete old log files to free up disk space"),
                         Action.FREE_DISK_SPACE)

    def test_network_action(self):
        self.assertEqual(map_action("Reconnect the container to its Docker network"),
                         Action.RECONNECT_NETWORK)
        self.assertEqual(map_action("Reconnect the network and restart the container"),
                         Action.UNKNOWN_NEEDS_HUMAN)
        self.assertEqual(map_action("Restart the container"), Action.RESTART_CONTAINER)

    def test_compound_disk_proposal_is_unknown(self):
        # The slow tier's real, honest proposal: two different actions joined by "or".
        text = "Increase the size of the data volume or remove unnecessary data"
        self.assertEqual(map_action(text), Action.UNKNOWN_NEEDS_HUMAN)

    def test_negation_and_junk_are_unknown(self):
        for text in ("Do not delete the data", "Rotate the TLS certs", "", None, 42):
            self.assertEqual(map_action(text), Action.UNKNOWN_NEEDS_HUMAN, repr(text))

    def test_memory_proposal_is_not_read_as_disk(self):
        self.assertEqual(map_action("Raise the memory limit so Postgres can free up memory"),
                         Action.INCREASE_MEMORY_LIMIT)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = executor.load_policy()

    def test_every_action_has_a_policy_entry(self):
        for a in Action:
            self.assertIn(a.value, self.policy, f"{a.value} missing from policy.yaml")

    def test_data_and_capacity_actions_are_never_auto(self):
        for a in ("INCREASE_DISK", "FREE_DISK_SPACE", "INCREASE_MEMORY_LIMIT",
                  "UNKNOWN_NEEDS_HUMAN", "NO_ACTION"):
            allowed, _ = executor.check_policy(a, 1.0, self.policy)
            self.assertFalse(allowed, f"{a} must stay human-approved")

    def test_reconnect_network_needs_confidence(self):
        self.assertTrue(executor.check_policy("RECONNECT_NETWORK", 0.9, self.policy)[0])
        self.assertFalse(executor.check_policy("RECONNECT_NETWORK", 0.5, self.policy)[0])

    def test_unlisted_action_is_blocked(self):
        allowed, _ = executor.check_policy("DROP_DATABASE", 1.0, self.policy)
        self.assertFalse(allowed)

    def test_live_execute_of_disk_actions_does_nothing(self):
        for a in ("INCREASE_DISK", "FREE_DISK_SPACE"):
            out = executor.execute(a, 1.0, "opsmind-postgres", dry_run=False, policy=self.policy)
            self.assertEqual(out["status"], "blocked_pending_human_approval")


if __name__ == "__main__":
    unittest.main()

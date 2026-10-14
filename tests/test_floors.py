"""Tests that protect the deterministic floors in agents/fast_tier.apply_floor.

If someone weakens a floor (higher threshold, dropped condition, lower severity), one of these
fails. No Docker, Postgres or Ollama needed: apply_floor is a pure function.

Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

import fast_tier as ft

# What a small model typically says for a real incident: "nothing to see here".
LAZY = {"severity": 0.1, "escalate": False, "confidence": 0.9}


def sig(**kw):
    """A quiet signal (no fault), with overrides."""
    base = {"oom_kill_delta": 0, "window_seconds": 30, "db_connect_ok": True}
    base.update(kw)
    return base


class FloorForcesEscalation(unittest.TestCase):
    def assertForced(self, signal):
        final, overridden = ft.apply_floor(dict(LAZY), signal)
        self.assertTrue(final["escalate"], "floor must force escalate=True")
        self.assertGreaterEqual(final["severity"], 0.8, "floor must raise severity to >= 0.8")
        self.assertTrue(overridden, "overridden must be True when the floor changed the decision")

    def assertNotForced(self, signal):
        final, overridden = ft.apply_floor(dict(LAZY), signal)
        self.assertEqual(final, LAZY)
        self.assertFalse(overridden)

    # ---- OOM ----
    def test_oom_kill_forces(self):
        self.assertForced(sig(oom_kill_delta=1))

    def test_no_oom_no_force(self):
        self.assertNotForced(sig(oom_kill_delta=0))

    # ---- CPU: needs BOTH share >= 0.8 and throttling ----
    def test_cpu_share_and_throttle_forces(self):
        self.assertForced(sig(cpu_share_of_limit=0.8, cpu_throttled_periods=1))

    def test_cpu_high_but_not_throttled_does_not_force(self):
        self.assertNotForced(sig(cpu_share_of_limit=0.99, cpu_throttled_periods=0))

    def test_cpu_throttled_but_below_share_does_not_force(self):
        self.assertNotForced(sig(cpu_share_of_limit=0.79, cpu_throttled_periods=50))

    def test_cpu_share_none_does_not_force(self):
        self.assertNotForced(sig(cpu_share_of_limit=None, cpu_throttled_periods=50))

    # ---- Disk: write_failed True OR usage >= 0.95 ----
    def test_write_failed_forces_even_with_low_usage(self):
        self.assertForced(sig(write_failed=True, data_disk_percent_used=0.10))

    def test_disk_usage_at_threshold_forces(self):
        self.assertForced(sig(data_disk_percent_used=0.95, write_failed=False))

    def test_disk_usage_just_below_threshold_does_not_force(self):
        self.assertNotForced(sig(data_disk_percent_used=0.949, write_failed=False))

    def test_write_probe_could_not_run_is_not_a_failure(self):
        # None means "probe did not run", which is not the same as "the write failed".
        self.assertNotForced(sig(write_failed=None, data_disk_percent_used=0.30))

    def test_disk_usage_unreadable_does_not_force(self):
        self.assertNotForced(sig(write_failed=False, data_disk_percent_used=None))

    # ---- Older records without CPU/disk keys must still work ----
    def test_old_oom_only_record_without_new_keys(self):
        self.assertForced({"oom_kill_delta": 2, "window_seconds": 30, "db_connect_ok": True})

    def test_old_quiet_record_without_new_keys(self):
        self.assertNotForced({"oom_kill_delta": 0, "window_seconds": 30, "db_connect_ok": True})

    # ---- The floor only raises: it never lowers a model's own severity ----
    def test_floor_keeps_higher_model_severity(self):
        model = {"severity": 0.95, "escalate": True, "confidence": 0.9}
        final, overridden = ft.apply_floor(model, sig(oom_kill_delta=1))
        self.assertEqual(final["severity"], 0.95)
        self.assertFalse(overridden)

    def test_floor_values_are_not_weakened(self):
        self.assertGreaterEqual(ft.OOM_SEVERITY_FLOOR, 0.8)
        self.assertGreaterEqual(ft.CPU_SEVERITY_FLOOR, 0.8)
        self.assertGreaterEqual(ft.DISK_SEVERITY_FLOOR, 0.8)
        self.assertLessEqual(ft.CPU_SHARE_THRESHOLD, 0.8)
        self.assertLessEqual(ft.DISK_USED_THRESHOLD, 0.95)


if __name__ == "__main__":
    unittest.main()

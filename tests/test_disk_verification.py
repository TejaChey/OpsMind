"""Tests for the disk-specific verification check (verify/health_check._check_disk).

The measurements are stubbed, so no Docker/Postgres is needed. The point: "container healthy"
is never enough; the write probe AND the usage threshold must both pass, and anything that
cannot be measured fails closed.

Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verify import health_check as hc

ARGS = ("c", "h", 1, "u", "p", "d")


def check(state=None, exc=None, threshold=0.9):
    with mock.patch.object(hc, "_disk_state", return_value=state, side_effect=exc):
        return hc._check_disk(*ARGS, threshold)


class DiskVerification(unittest.TestCase):
    def test_passes_only_when_write_ok_and_usage_low(self):
        ok, fields, _ = check((0.475, False, None))
        self.assertTrue(ok)
        self.assertEqual(fields["disk_percent_used_after"], 0.475)

    def test_fails_while_write_still_failing(self):
        ok, _, why = check((0.475, True, "No space left on device"))
        self.assertFalse(ok)
        self.assertIn("still fails", why)

    def test_fails_when_write_ok_but_volume_still_nearly_full(self):
        self.assertFalse(check((0.93, False, None))[0])

    def test_usage_exactly_at_threshold_fails(self):
        self.assertFalse(check((0.9, False, None))[0])

    def test_fails_closed_when_probe_could_not_run(self):
        self.assertFalse(check((0.2, None, "probe could not connect"))[0])

    def test_fails_closed_when_usage_unreadable(self):
        self.assertFalse(check((None, False, None))[0])

    def test_fails_closed_on_exception(self):
        ok, _, why = check(exc=RuntimeError("docker exploded"))
        self.assertFalse(ok)
        self.assertIn("not measurable", why)


def net(results, threshold=1.0):
    """Run _check_network with stubbed (ok, err) probe results, one per probe."""
    with mock.patch.object(hc, "_select_one", side_effect=results):
        return hc._check_network("h", 1, "u", "p", "d", threshold, probes=len(results))


class NetworkVerification(unittest.TestCase):
    def test_passes_when_all_probes_fast(self):
        self.assertTrue(net([(True, None)] * 3)[0])

    def test_fails_when_any_probe_fails(self):
        ok, _, why = net([(True, None), (False, "timeout"), (True, None)])
        self.assertFalse(ok)
        self.assertIn("probe failed", why)

    def test_fails_when_reachable_but_slow(self):
        with mock.patch.object(hc.time, "monotonic", side_effect=[0, 2.0, 2.0, 2.1, 2.1, 2.2]):
            ok, _, why = net([(True, None)] * 3)
        self.assertFalse(ok)
        self.assertIn("slow", why)

    def test_unexpected_result_fails(self):
        self.assertFalse(net([(False, None)])[0])


if __name__ == "__main__":
    unittest.main()

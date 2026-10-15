import sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from verify import health_check  # noqa: E402

ARGS = dict(host="h", port=1, user="u", password="p", dbname="d")


def run(samples, resamples=2):
    """Verify with a scripted sequence of CPU readings; returns (result, how many samples were taken)."""
    seq = iter(samples)
    taken = []

    def fake_cpu(container, window):
        v = next(seq)
        taken.append(v)
        return (None, "cgroup unreadable") if v is None else (v, None)

    with mock.patch.object(health_check, "_container_health", return_value=("healthy", None)), \
         mock.patch.object(health_check, "_select_one", return_value=(True, None)), \
         mock.patch.object(health_check, "_cpu_share", side_effect=fake_cpu):
        res = health_check.verify("c", **ARGS, expected_signal="cpu", cpu_window=0,
                                  cpu_resamples=resamples, timeout=5)
    return res, len(taken)


class CpuVerification(unittest.TestCase):
    def test_low_first_sample_passes_without_resampling(self):
        res, n = run([0.1])
        self.assertTrue(res["verified"])
        self.assertEqual(n, 1)
        self.assertEqual(res["cpu_samples"], [0.1])

    def test_tail_of_killed_query_recovers_on_resample(self):
        res, n = run([0.9, 0.2])
        self.assertTrue(res["verified"], res["reason"])
        self.assertEqual(res["cpu_samples"], [0.9, 0.2])
        self.assertEqual(res["cpu_share_after"], 0.2)

    def test_persistent_load_still_fails_after_all_resamples(self):
        res, n = run([0.9, 0.8, 0.95])
        self.assertFalse(res["verified"])
        self.assertEqual(n, 3)                     # 1 + 2 resamples, then stop
        self.assertIn("did not recover", res["reason"])

    def test_stops_sampling_once_a_sample_is_low(self):
        res, n = run([0.9, 0.2, 0.9])              # the third reading must never be taken
        self.assertTrue(res["verified"])
        self.assertEqual(n, 2)

    def test_unmeasurable_fails_closed_and_is_not_retried(self):
        res, n = run([None])
        self.assertFalse(res["verified"])
        self.assertEqual(n, 1)

    def test_unmeasurable_after_a_high_sample_fails_closed(self):
        res, n = run([0.9, None])
        self.assertFalse(res["verified"])
        self.assertIsNone(res["cpu_share_after"])

    def test_zero_resamples_is_the_old_single_sample_behaviour(self):
        res, n = run([0.9, 0.1], resamples=0)
        self.assertFalse(res["verified"])
        self.assertEqual(n, 1)

    def test_threshold_is_strict(self):
        res, _ = run([0.5, 0.5, 0.5])
        self.assertFalse(res["verified"])


if __name__ == "__main__":
    unittest.main()

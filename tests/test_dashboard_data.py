import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import data  # noqa: E402


def run(**kw):
    base = {"run_id": "abcdef123456", "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "escalate_slow_tier", "decision": {"severity": 0.9}}
    base.update(kw)
    return base


class DashboardData(unittest.TestCase):
    def test_stats_counts_only_real_live_executions(self):
        runs = [
            run(overridden=True, execution={"status": "executed", "dry_run": False},
                verification={"verified": True}),
            run(execution={"status": "executed", "dry_run": False}, verification={"verified": False}),
            run(execution={"status": "executed", "dry_run": True}),   # dry run: not live
            run(action="no_action"),
        ]
        s = data.stats(runs)
        self.assertEqual(s["total_runs"], 4)
        self.assertEqual(s["overridden"], 1)
        self.assertEqual(s["live_executed"], 2)
        self.assertEqual(s["live_verified"], 1)

    def test_load_jsonl_keeps_corrupt_line_visible(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            p.write_text('{"run_id": "a"}\n\nnot json\n')
            rows = data.load_jsonl(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["action"], "CORRUPT")

    def test_missing_file_is_empty(self):
        self.assertEqual(data.load_jsonl("/nonexistent/file.jsonl"), [])

    def test_summary_handles_missing_stages(self):
        row = data.run_summary(run())
        self.assertIsNone(row["verified"])
        self.assertEqual(row["run_id"], "abcdef12")

    def test_real_file_matches_line_count(self):
        n = sum(1 for l in data.RUNS.read_text().splitlines() if l.strip())
        self.assertEqual(data.stats(data.load_jsonl(data.RUNS))["total_runs"], n)


if __name__ == "__main__":
    unittest.main()


class HandOffQueue(unittest.TestCase):
    def test_open_until_a_later_verified_fix(self):
        from data import open_escalations
        esc = [{"queued_at": "2026-11-06T10:00:00", "run_id": "a"},
               {"queued_at": "2026-11-06T12:00:00", "run_id": "b"}]
        runs = [{"timestamp": "2026-11-06T11:00:00", "verification": {"verified": True}},
                {"timestamp": "2026-11-06T13:00:00", "verification": {"verified": False}}]
        out = {e["run_id"]: e["status"] for e in open_escalations(esc, runs)}
        self.assertEqual(out, {"a": "fixed later", "b": "OPEN"})

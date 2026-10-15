import json, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.jsonl_io import append_jsonl  # noqa: E402

try:
    import fcntl
except ImportError:  # Windows: lock behaviour is not tested here
    fcntl = None


class AppendJsonl(unittest.TestCase):
    def test_creates_parent_dir_and_ends_with_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a" / "b.jsonl"
            append_jsonl(p, {"k": 1})
            append_jsonl(p, {"k": 2})
            self.assertEqual([json.loads(l) for l in p.read_text().splitlines()], [{"k": 1}, {"k": 2}])

    @unittest.skipIf(fcntl is None, "POSIX lock test")
    def test_append_waits_while_another_process_holds_the_lock(self):
        """Deterministic: we hold the lock; the writer must not write until we let go."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            p.write_text("")
            code = (f"import sys; sys.path.insert(0, {str(ROOT)!r});"
                    f"from tools.jsonl_io import append_jsonl; append_jsonl({str(p)!r}, {{'k': 1}})")
            with p.open("a") as holder:
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
                proc = subprocess.Popen([sys.executable, "-c", code])
                time.sleep(1.0)
                self.assertIsNone(proc.poll(), "writer finished while the lock was held")
                self.assertEqual(p.read_text(), "", "writer wrote while the lock was held")
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            self.assertEqual(proc.wait(timeout=10), 0)
            self.assertEqual(p.read_text(), '{"k": 1}\n')


if __name__ == "__main__":
    unittest.main()

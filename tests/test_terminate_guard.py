"""Real-Postgres test of the TERMINATE_QUERY guard (min age + own-user only).
Skips itself when the compose Postgres is not reachable. Starts only its own pg_sleep queries and a
temporary role, and cleans both up."""
import sys, threading, time, unittest
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import executor  # noqa: E402


def _env():
    cfg = {}
    for line in (ROOT / "services" / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def _db(cfg, user=None, password=None):
    return {"host": "localhost", "port": int(cfg["POSTGRES_HOST_PORT"]), "dbname": cfg["POSTGRES_DB"],
            "user": user or cfg["POSTGRES_USER"], "password": password or cfg["POSTGRES_PASSWORD"]}


def _sleeper(db, seconds, started):
    conn = psycopg2.connect(connect_timeout=5, **db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        started.append(cur.fetchone()[0])
        try:
            cur.execute(f"SELECT pg_sleep({seconds})")
        except psycopg2.Error:
            pass  # terminated by the test cleanup
    conn.close()


class TerminateGuard(unittest.TestCase):
    OTHER = "opsmind_guard_test_role"

    @classmethod
    def setUpClass(cls):
        try:
            cls.cfg = _env()
            cls.db = _db(cls.cfg)
            admin = psycopg2.connect(connect_timeout=3, **cls.db)
        except Exception as e:  # no .env or Postgres down
            raise unittest.SkipTest(f"Postgres not reachable: {e}")
        admin.autocommit = True
        cls.admin = admin
        with admin.cursor() as cur:
            cur.execute(f"DROP ROLE IF EXISTS {cls.OTHER}")
            cur.execute(f"CREATE ROLE {cls.OTHER} LOGIN PASSWORD 'x'")

    @classmethod
    def tearDownClass(cls):
        with cls.admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
                        (cls.OTHER,))
            cur.execute(f"DROP ROLE IF EXISTS {cls.OTHER}")
        cls.admin.close()

    def _start(self, db, seconds=60):
        started = []
        t = threading.Thread(target=_sleeper, args=(db, seconds, started), daemon=True)
        t.start()
        while not started:
            time.sleep(0.05)
        time.sleep(0.3)  # let the sleep statement begin
        self.addCleanup(self._kill, started[0])
        return started[0]

    def _kill(self, pid):
        with self.admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (pid,))

    def test_young_query_is_not_a_target_then_old_enough_is(self):
        pid = self._start(self.db)
        status, reason, ev = executor.terminate_query(self.db, dry_run=True)
        self.assertEqual(status, "no_target", reason)
        self.assertEqual(ev["skipped_candidate"]["pid"], pid)
        time.sleep(executor.MIN_TARGET_SECONDS)
        status, reason, ev = executor.terminate_query(self.db, dry_run=True)
        self.assertEqual(status, "would_execute", reason)
        self.assertEqual(ev["target_before"]["pid"], pid)

    def test_other_users_old_query_is_never_a_target(self):
        other = _db(self.cfg, user=self.OTHER, password="x")
        pid = self._start(other)
        time.sleep(executor.MIN_TARGET_SECONDS + 1)
        status, reason, ev = executor.terminate_query(self.db, dry_run=True)
        self.assertEqual(status, "no_target", reason)
        self.assertEqual(ev["skipped_candidate"]["pid"], pid)
        self.assertEqual(ev["skipped_candidate"]["usename"], self.OTHER)

    def test_live_terminate_of_own_old_query_confirms_pid_gone(self):
        pid = self._start(self.db)
        time.sleep(executor.MIN_TARGET_SECONDS + 1)
        status, reason, ev = executor.terminate_query(self.db, dry_run=False)
        self.assertEqual(status, "executed", reason)
        self.assertEqual(ev["target_before"]["pid"], pid)
        self.assertTrue(ev["pid_gone_after"])


if __name__ == "__main__":
    unittest.main()

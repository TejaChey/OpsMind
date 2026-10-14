"""Tests for the OKF knowledge layer (tools/knowledge.py). A temp bundle is used each time.

What they protect: only verified REAL runs are recorded, writing is idempotent and keeps
history, provenance (sources/verified/footnotes) is complete, trust is only 'machine-confirmed'
for code-verified facts, and the bundle stays conformant with OKF v0.2.

Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import knowledge as kn

RUN = "aaaaaaaa-1111-2222-3333-444444444444"


def record(run_id=RUN, **over):
    rec = {
        "run_id": run_id, "timestamp": "2026-10-14T13:36:14.051631+00:00", "overridden": True,
        "signal": {"container_state": "exited", "app_health_status": 503, "oom_kill_delta": None},
        "slow_tier": {"diagnosis": {"proposed_fix": "Restart the database container"}},
        "mapped_action": "RESTART_DEPENDENCY",
        "execution": {"status": "executed", "dry_run": False},
        "verification": {"verified": True, "reason": "healthy and app 200"},
    }
    rec.update(over)
    return rec


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.bundle = Path(d.name) / "knowledge"
        kn.seed(self.bundle)

    def node(self, slug):
        return kn.read_concept(self.bundle / "faults" / f"{slug}.md")

    # ---- what may be recorded ----
    def test_verified_real_run_is_recorded_in_the_right_node(self):
        self.assertEqual(kn.record_incident(record(), self.bundle), ("recorded", "dependency-down"))
        fm, body = self.node("dependency-down")
        self.assertEqual(fm["sources"][0]["id"], "run-aaaaaaaa")
        self.assertIn(RUN, fm["sources"][0]["resource"])
        self.assertIn("[^run-aaaaaaaa]:", body)

    def test_refusals_write_nothing(self):
        bad = [record(HARNESS_TEST=True),
               record(execution={"status": "executed", "dry_run": True}),
               record(execution={"status": "blocked_pending_human_approval", "dry_run": False}),
               record(verification={"verified": False, "reason": "x"}),
               record(verification=None),
               record(signal={"container_state": "running", "app_health_status": 200})]
        for r in bad:
            self.assertEqual(kn.record_incident(r, self.bundle)[0], "refused", r)
        self.assertEqual(kn.load_outcomes("dependency-down", self.bundle), [])
        self.assertNotIn("Recorded verified run", (self.bundle / "log.md").read_text())

    def test_same_run_twice_is_a_noop(self):
        kn.record_incident(record(), self.bundle)
        self.assertEqual(kn.record_incident(record(), self.bundle)[0], "already_recorded")
        self.assertEqual(len(kn.load_outcomes("dependency-down", self.bundle)), 1)

    def test_log_keeps_all_earlier_entries(self):
        # Regression: the log once dropped older entries because bullets were not preserved.
        kn.record_incident(record("11111111-0000-0000-0000-000000000000"), self.bundle)
        kn.record_incident(record("22222222-0000-0000-0000-000000000000"), self.bundle)
        log = (self.bundle / "log.md").read_text()
        for needle in ("Initialization", "11111111", "22222222"):
            self.assertIn(needle, log)

    # ---- trust and conformance ----
    def test_trust_tier_is_machine_confirmed_never_human(self):
        self.assertEqual(kn.trust_tier(self.node("dependency-down")[0]), "unverified")
        kn.record_incident(record(), self.bundle)
        self.assertEqual(kn.trust_tier(self.node("dependency-down")[0]), "machine-confirmed")
        self.assertEqual(kn.trust_tier({"verified": {"by": "human:teja", "at": "x"}}), "human-reviewed")

    def test_seeded_and_recorded_bundle_is_conformant(self):
        self.assertEqual(kn.validate(self.bundle), [])
        kn.record_incident(record(), self.bundle)
        self.assertEqual(kn.validate(self.bundle), [])

    def test_validator_catches_problems(self):
        (self.bundle / "faults" / "notype.md").write_text("---\ntitle: x\n---\nbody\n")
        (self.bundle / "faults" / "nofm.md").write_text("just text\n")
        (self.bundle / "faults" / "orphan.md").write_text(
            "---\ntype: X\n---\nclaim.[^ghost]\n\n[^ghost]: nope\n")
        (self.bundle / "log.md").write_text("# Log\n\n## 2026-01-01\n* a\n\n## 2026-02-01\n* b\n")
        text = "\n".join(kn.validate(self.bundle))
        for needle in ("notype.md: missing non-empty `type`", "nofm.md: no YAML frontmatter",
                       "footnote [^ghost]", "newest first"):
            self.assertIn(needle, text)

    def test_index_carries_okf_version(self):
        self.assertIn('okf_version: "0.2"', (self.bundle / "index.md").read_text())

    # ---- fault matching is deterministic code ----
    def test_matched_faults(self):
        m = kn.matched_faults
        self.assertEqual(m({"oom_kill_delta": 1}), ["oom"])
        self.assertEqual(m({"cpu_share_of_limit": 0.9, "cpu_throttled_periods": 3}), ["cpu-spike"])
        self.assertEqual(m({"data_disk_percent_used": 0.97}), ["disk-full"])
        self.assertEqual(m({"db_connect_ok": False, "pg_ready_inside": True}), ["network-partition"])
        self.assertEqual(m({"container_state": "running", "app_health_status": 503,
                            "last_deploy": {"status_when_deployed": "bad"}}), ["config-regression"])
        self.assertEqual(m({"container_state": "exited", "oom_kill_delta": 1}),
                         ["dependency-down", "oom"])   # priority order, both reported
        self.assertEqual(m({"container_state": "running", "app_health_status": 200}), [])


class SupervisorHookPlacement(unittest.TestCase):
    def test_knowledge_hook_is_not_inside_run_once(self):
        # tools/harness_stub_restart.py calls run_once() with forced inputs and labels the
        # record HARNESS_TEST only afterwards, so the hook must live in main(), not run_once().
        import inspect
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
        import supervisor
        # No calls into the knowledge module at all inside run_once (the `knowledge_context`
        # parameter, which only carries what the slow tier is shown, has no dot after "knowledge").
        self.assertNotIn("knowledge.", inspect.getsource(supervisor.run_once))
        self.assertNotIn("record_incident", inspect.getsource(supervisor.run_once))
        self.assertIn("knowledge.record_incident", inspect.getsource(supervisor.main))


if __name__ == "__main__":
    unittest.main()

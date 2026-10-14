"""Tests for feeding knowledge to the slow tier: no answer key leaks into the context,
leave-one-out works, and the default slow-tier request is unchanged when no knowledge is given.

Run from the repo root:  python -m unittest discover tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT))

import slow_tier
from tools import knowledge as kn
from test_knowledge import record

CFG_SIGNAL = {"container_state": "running", "app_health_status": 503,
              "last_deploy": {"status_when_deployed": "bad"}}


class ContextTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.bundle = Path(d.name) / "knowledge"
        kn.seed(self.bundle)
        for rid in ("11111111-0000-0000-0000-000000000000", "22222222-0000-0000-0000-000000000000"):
            kn.record_incident(record(rid, signal=CFG_SIGNAL, mapped_action="ROLLBACK_CONFIG"),
                               self.bundle)

    def test_no_answer_key_in_the_context(self):
        text = json.dumps(kn.context_for(CFG_SIGNAL, self.bundle))
        banned = [kn.FAULTS["config-regression"]["correct_fix"], "ground truth", "Findings",
                  "auto-approve", "human approval", "forces escalate", "ROLLBACK_CONFIG",
                  "RESTART_DEPENDENCY", "RECONNECT_NETWORK", "INCREASE_"]
        for b in banned:
            self.assertNotIn(b, text, b)

    def test_context_carries_past_verified_experience(self):
        ctx = kn.context_for(CFG_SIGNAL, self.bundle)
        self.assertEqual(len(ctx["previous_verified_incidents"]), 2)
        self.assertTrue(all(p["was_executed"] for p in ctx["previous_verified_incidents"]))

    def test_leave_one_out_removes_the_runs_own_outcome(self):
        ctx = kn.context_for(CFG_SIGNAL, self.bundle,
                             exclude_run_id="11111111-0000-0000-0000-000000000000")
        self.assertEqual(len(ctx["previous_verified_incidents"]), 1)

    def test_fault_with_no_experience_says_so(self):
        ctx = kn.context_for({"oom_kill_delta": 1}, self.bundle)
        self.assertEqual(ctx["previous_verified_incidents"], [])

    def test_no_match_gives_none(self):
        self.assertIsNone(kn.context_for({"container_state": "running"}, self.bundle))

    def test_forced_fault_node(self):
        ctx = kn.context_for({"oom_kill_delta": 1}, self.bundle, fault="config-regression")
        self.assertEqual(ctx["incident_type"], "Config regression after deploy")


class SlowTierRequest(unittest.TestCase):
    FAST = {"signal": {"oom_kill_delta": 0, "window_seconds": 3, "db_connect_ok": True},
            "model_raw": {"severity": 0.1, "escalate": False, "confidence": 0.9},
            "decision": {"severity": 0.9, "escalate": True, "confidence": 0.9}, "overridden": True}

    def payload(self, **kw):
        resp = mock.Mock()
        resp.json.return_value = {"message": {"content": "{}"}}
        with mock.patch.object(slow_tier.requests, "post", return_value=resp) as post:
            slow_tier.ask_model("m", self.FAST, **kw)
        return post.call_args.kwargs["json"]["messages"]

    def test_default_request_is_unchanged(self):
        msgs = self.payload()
        self.assertEqual(msgs[0]["content"], slow_tier.SYSTEM_PROMPT)
        self.assertNotIn("knowledge_base", msgs[1]["content"])

    def test_knowledge_is_added_as_facts_plus_a_note(self):
        msgs = self.payload(knowledge={"incident_type": "x"})
        self.assertIn("knowledge_base", msgs[1]["content"])
        self.assertTrue(msgs[0]["content"].startswith(slow_tier.SYSTEM_PROMPT))
        self.assertIn("PREVIOUS incidents", msgs[0]["content"])

    def test_system_prompt_override(self):
        self.assertEqual(self.payload(system_prompt="custom")[0]["content"], "custom")


if __name__ == "__main__":
    unittest.main()

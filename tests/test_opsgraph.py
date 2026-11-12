"""The multi-agent graph (opsgraph/) with models, Docker and the executor faked: grounding,
routing, code review of plans, gate checks and ordering, revision loop, human interrupt/resume.
No Ollama or Docker needed."""
import json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "agents")]
from langgraph.types import Command  # noqa: E402
from opsgraph import graph as G, specialists as SP  # noqa: E402

CACHE_DOWN = {"app_health_status": 503, "app_health_error": "redis: connection refused",
              "redis_container_state": "exited", "app_redis_ok": False, "app_db_ok": True,
              "container_state": "running", "app_container_state": "running", "db_connect_ok": True}
COMPOUND = {**CACHE_DOWN, "app_db_ok": False,
            "last_deploy": {"status_when_deployed": "bad", "changed_vs_last_known_good": {"db_port": [5432, 5439]}}}


def finding(comp, status, grounded=True):
    return {"component": comp, "status": status, "grounded": grounded, "summary": "x",
            "evidence": ["e-1"] if grounded else [], "confidence": 0.9}


class Grounding(unittest.TestCase):
    def test_faulty_without_real_citation_is_downgraded(self):
        f = SP.ground({"status": "faulty", "summary": "broken", "evidence": ["db-9"], "confidence": 0.9},
                      {"db-1": "..."})
        self.assertEqual((f["status"], f["grounded"]), ("unknown", False))
        self.assertEqual(f["invalid_citations"], ["db-9"])

    def test_cited_faulty_stays_faulty(self):
        f = SP.ground({"status": "faulty", "summary": "down", "evidence": ["[db-1]"], "confidence": 2},
                      {"db-1": "..."})
        self.assertEqual((f["status"], f["grounded"], f["confidence"]), ("faulty", True, 1.0))

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            SP.ground({"status": "on fire", "summary": "x"}, {})

    def test_tool_validation_blocks_other_components_and_free_sql(self):
        tb = mock.Mock()
        specs = SP.SPECIALISTS["cache"]["tools"]
        self.assertIn("not available", SP.call_tool(tb, specs, "db_query", {"name": "settings"}))
        self.assertIn("must be one of", SP.call_tool(tb, specs, "container_logs", {"container": "opsmind-postgres"}))
        db = SP.SPECIALISTS["database"]["tools"]
        self.assertIn("must be one of", SP.call_tool(tb, db, "db_query", {"name": "DROP TABLE x"}))
        tb.assert_not_called()

    def test_specialists_only_see_their_own_measurements(self):
        f = SP.scoped_facts("database", COMPOUND)
        self.assertNotIn("app_health_error", f)
        self.assertNotIn("last_deploy", f)
        self.assertIn("last_deploy", SP.scoped_facts("application", COMPOUND))

    def test_no_specialist_has_a_write_tool(self):
        for name, s in SP.SPECIALISTS.items():
            for t in s["tools"]:
                self.assertIn(t["name"], {"list_containers", "container_logs", "db_query",
                                          "disk_usage", "network_info", "app_health",
                                          "deploy_history", "cache_info"}, name)


class Review(unittest.TestCase):
    def test_uncovered_faulty_cause_is_an_objection(self):
        plan = {"steps": [{"component": "application", "fix": "Roll back the web app's config"}]}
        obj = G.coverage_objections(COMPOUND, [finding("cache", "faulty"), finding("application", "faulty")], plan)
        self.assertTrue(any("cache" in o for o in obj))

    def test_symptom_app_does_not_need_a_step(self):
        plan = {"steps": [{"component": "cache", "fix": "Restart the redis cache container"}]}
        self.assertEqual(G.coverage_objections(CACHE_DOWN, [finding("cache", "faulty"),
                                                            finding("application", "faulty")], plan), [])

    def test_bad_label_without_a_config_difference_is_not_a_cause(self):
        sig = {**CACHE_DOWN, "last_deploy": {"status_when_deployed": "bad", "changed_vs_last_known_good": {}}}
        plan = {"steps": [{"component": "cache", "fix": "Restart the redis cache container"}]}
        self.assertEqual(G.coverage_objections(sig, [finding("cache", "faulty"), finding("application", "faulty")], plan), [])

    def test_step_must_act_on_its_component(self):
        plan = {"steps": [{"component": "cache", "fix": "Reconnect the container to its Docker network"}]}
        obj = G.coverage_objections(CACHE_DOWN, [finding("cache", "faulty")], plan)
        self.assertTrue(any("RECONNECT_NETWORK" in o for o in obj))

    def test_requirements_come_from_measurements_not_agent_opinions(self):
        partition = {"db_connect_ok": False, "pg_ready_inside": True, "db_connect_error": "timeout expired",
                     "container_state": "running", "app_health_status": 503}
        plan = {"steps": [{"component": "network", "fix": "Reconnect the container to its Docker network"}]}
        # the database agent could not connect and called the DB faulty: that alone demands nothing
        self.assertEqual(G.coverage_objections(partition, [finding("database", "faulty"), finding("network", "faulty")], plan), [])
        # a measured fault (stopped cache) is demanded even if no agent reported it
        none_plan = {"steps": [{"component": "none", "fix": "Flag for human review"}]}
        self.assertTrue(G.coverage_objections(CACHE_DOWN, [], none_plan))
        # connection exhaustion: the server answered (FATAL), so it is not a network cause
        exhaust = {**partition, "db_connect_error": "FATAL: sorry, too many clients already"}
        self.assertNotIn("network", G.measured_causes(exhaust))

    def test_validate_plan(self):
        ok = G.validate_plan(json.dumps({"root_cause": "r", "steps": [{"component": "cache", "fix": "f"}], "confidence": 0.8}))
        self.assertEqual(ok["steps"][0]["component"], "cache")
        for bad in ({"root_cause": "r", "steps": [], "confidence": 0.5},
                    {"root_cause": "r", "steps": [{"component": "x", "fix": "a"}] * 3, "confidence": 0.5},
                    {"root_cause": "r", "steps": [{"component": "cache", "fix": "a"}], "confidence": 3}):
            with self.assertRaises(ValueError):
                G.validate_plan(json.dumps(bad))

    def test_routing_is_code(self):
        st = {"run_id": "t", "cfg": {"knowledge": False, "events_dir": tempfile.mkdtemp()}}
        r = G.route({**st, "signal": CACHE_DOWN})["route"]
        self.assertIn("cache", r)
        self.assertIn("application", r)
        self.assertNotIn("network", G.route({**st, "signal": {**CACHE_DOWN, "app_health_error": "x"}})["route"])
        part = {"db_connect_ok": False, "pg_ready_inside": True, "app_health_status": 503}
        self.assertIn("network", G.route({**st, "signal": part})["route"])


class FullGraph(unittest.TestCase):
    """The whole graph with every model call scripted and the executor faked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = G.default_cfg(events_dir=self.tmp, record=False, knowledge=False, dry_run=True)
        self.exec_calls = []

        def fake_execute(action, conf, container, dry_run, **kw):
            self.exec_calls.append((action, dry_run))
            return {"action": action, "dry_run": dry_run, "container": container,
                    "status": "would_execute" if dry_run else "executed", "reason": "ok",
                    "targets": ["opsmind-redis"] if action == "RESTART_DEPENDENCY" else None}
        self.patches = [
            mock.patch.object(G.fast_tier, "classify", return_value={
                "decision": {"escalate": True, "severity": 0.9}, "overridden": True,
                "model_raw": "{}", "source": "model", "signal": COMPOUND}),
            mock.patch.object(G.executor, "execute", side_effect=fake_execute),
            mock.patch.object(G, "_topology", return_value={}),
            mock.patch.object(G.governor, "check", return_value=(True, "ok", {})),
            mock.patch.object(G.specialists, "investigate", side_effect=self.fake_specialist),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    faulty = {"cache": "faulty", "application": "faulty"}

    def fake_specialist(self, name, signal, toolbox, model, emit):
        status = self.faulty.get(name, "healthy")
        return {**finding(name, status), "trace": []}

    def run_graph(self, chat_replies, hitl=False, answer=None, signal=COMPOUND):
        replies = iter(chat_replies)
        with mock.patch.object(G.specialists, "_chat", side_effect=lambda *a, **k: {"content": json.dumps(next(replies))}):
            g = G.build()
            th = {"configurable": {"thread_id": "t1"}}
            cfg = {**self.cfg, "hitl": hitl}
            out = g.invoke({"run_id": "t1", "cfg": cfg, "revisions": 0, "signal": signal}, th)
            if "__interrupt__" in out:
                self.interrupted = out["__interrupt__"][0].value
                out = g.invoke(Command(resume=answer or {"approve": []}), th)
            return out

    def test_revision_loop_then_ordered_two_step_plan(self):
        one_step = {"root_cause": "bad deploy", "steps": [{"component": "application", "fix": "Roll back the web app's config to the last known-good deploy"}], "confidence": 0.9}
        two_step = {"root_cause": "bad deploy and cache down", "steps": [
            {"component": "application", "fix": "Roll back the web app's config to the last known-good deploy"},
            {"component": "cache", "fix": "Restart the redis cache container"}], "confidence": 0.9}
        approve = {"verdict": "approve", "unexplained_facts": [], "reason": "fine"}
        out = self.run_graph([one_step, approve, two_step, approve])
        crits = out["critiques"]
        self.assertEqual(crits[0]["verdict"], "revise")            # code overruled "approve"
        self.assertTrue(crits[0]["code_objections"])
        self.assertEqual(crits[1]["verdict"], "approve")
        self.assertEqual([s["action"] for s in out["plan"]], ["RESTART_DEPENDENCY", "ROLLBACK_CONFIG"])
        self.assertEqual({s["decision"] for s in out["plan"]}, {"auto"})
        self.assertEqual(out["outcome"]["status"], "dry_run_planned")
        self.assertEqual({f["component"] for f in out["findings"]}, set(out["route"]))

    def test_critic_model_alone_cannot_block_automation(self):
        plan = {"root_cause": "cache", "steps": [{"component": "cache", "fix": "Restart the redis cache container"}], "confidence": 0.9}
        nitpick = {"verdict": "revise", "unexplained_facts": ["DNS"], "reason": "DNS issue not addressed"}
        out = self.run_graph([plan, nitpick], signal=CACHE_DOWN)
        self.assertEqual(len(out["critiques"]), 1)                  # no revision loop
        self.assertEqual(out["critiques"][0]["model_verdict"], "revise")
        self.assertEqual(out["plan"][0]["decision"], "auto")
        adv = [c for c in out["plan"][0]["checks"] if c.get("advisory")]
        self.assertTrue(adv and "DNS" in adv[0]["detail"])           # but the objection is shown

    def test_human_interrupt_and_resume(self):
        self.faulty = {"database": "faulty"}
        plan = {"root_cause": "out of memory", "steps": [{"component": "database", "fix": "Increase the database container's memory limit"}], "confidence": 0.9}
        approve = {"verdict": "approve", "unexplained_facts": [], "reason": "fine"}
        out = self.run_graph([plan, approve], hitl=True, answer={"approve": [0], "by": "test"},
                             signal={"oom_kill_delta": 1, "app_health_status": 200})
        self.assertEqual(self.interrupted["steps"][0]["action"], "INCREASE_MEMORY_LIMIT")
        self.assertEqual(out["plan"][0]["decision"], "human_approved")
        self.assertEqual(out["executions"][0]["status"], "manual_action_required")
        self.assertNotIn(("INCREASE_MEMORY_LIMIT", False), self.exec_calls)   # never executed

    def test_invalid_commander_output_falls_back_to_a_human(self):
        approve = {"verdict": "approve", "unexplained_facts": [], "reason": "fine"}
        out = self.run_graph([{"nonsense": 1}, {"also": "bad"}, approve, approve, approve, approve])
        self.assertTrue(out["diagnosis"].get("fallback"))
        self.assertTrue(all(s["decision"] != "auto" for s in out["plan"]))

    def test_events_are_written_for_the_console(self):
        approve = {"verdict": "approve", "unexplained_facts": [], "reason": "fine"}
        plan = {"root_cause": "cache", "steps": [{"component": "cache", "fix": "Restart the redis cache container"}], "confidence": 0.9}
        self.run_graph([plan, approve], signal=CACHE_DOWN)
        types = [json.loads(l)["type"] for l in (Path(self.tmp) / "t1.jsonl").read_text().splitlines()]
        for t in ("signal", "decision", "route", "diagnosis", "critique", "gate", "action", "outcome"):
            self.assertIn(t, types)


if __name__ == "__main__":
    unittest.main()

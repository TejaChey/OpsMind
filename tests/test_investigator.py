"""The investigator agent's loop (agents/investigator.py) with a SCRIPTED model: tool-call
validation, budget, evidence challenge, schema validation, fallback. No Ollama/Docker."""
import json, sys, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))
import investigator as inv  # noqa: E402

GOOD = {"root_cause": "Postgres is in read-only mode", "proposed_fix": "flag for human review",
        "confidence": 0.8}


def call(tool, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": tool, "arguments": args}}]}


class FakeTools(inv.Toolbox):
    def __init__(self):
        self.calls = []

    def list_containers(self):
        self.calls.append("list_containers")
        return "opsmind-postgres state=running"

    def db_query(self, name):
        self.calls.append(("db_query", name))
        return [{"name": "default_transaction_read_only", "setting": "on"}]


def run(script, signal=None, max_steps=8):
    replies = iter(script)
    seen_messages = []

    def fake_chat(model, messages, tools=True, fmt=None):
        seen_messages.append([dict(m) for m in messages])
        return next(replies)
    fast = {"signal": signal or {}, "decision": {"escalate": True}, "overridden": True}
    tools = FakeTools()
    with mock.patch.object(inv, "_chat", side_effect=fake_chat):
        out = inv.investigate(fast, tools, max_steps=max_steps)
    return out, tools, seen_messages


class Loop(unittest.TestCase):
    def test_tools_then_submit(self):
        out, tools, _ = run([call("list_containers"), call("db_query", name="settings"),
                             call("submit_diagnosis", **GOOD)])
        self.assertEqual(out["source"], "model")
        self.assertEqual(out["diagnosis"]["root_cause"], GOOD["root_cause"])
        self.assertEqual([t["tool"] for t in out["trace"]], ["list_containers", "db_query"])
        self.assertEqual(tools.calls, ["list_containers", ("db_query", "settings")])

    def test_bad_tool_arguments_never_reach_the_tool(self):
        out, tools, _ = run([call("db_query", name="DROP TABLE x"), call("rm_rf"),
                             call("container_logs", container="/etc/passwd"),
                             call("container_logs"), call("submit_diagnosis", **GOOD)])
        self.assertEqual(tools.calls, [])
        results = [t["result"] for t in out["trace"]]
        self.assertTrue(all(r.startswith("error:") for r in results), results)

    def test_budget_forces_a_schema_constrained_conclusion(self):
        script = [call("list_containers")] * 3 + [{"content": json.dumps(GOOD)}]
        out, _, _ = run(script, max_steps=3)
        self.assertTrue(out["forced_conclusion"])
        self.assertEqual(out["source"], "model")

    def test_invalid_submission_is_rejected_then_accepted(self):
        out, _, _ = run([call("submit_diagnosis", root_cause="x", proposed_fix="y", confidence=7),
                         call("submit_diagnosis", **GOOD)])
        self.assertEqual(out["diagnosis"]["confidence"], 0.8)
        self.assertIn("out of range", out["attempts"][0]["error"])

    def test_evidence_challenge_happens_once_when_evidence_exists(self):
        sig = {"write_failed": True, "write_error": "cannot execute INSERT in a read-only transaction"}
        out, _, msgs = run([call("submit_diagnosis", **GOOD), call("db_query", name="settings"),
                            call("submit_diagnosis", **GOOD)], signal=sig)
        self.assertEqual(out["source"], "model")
        self.assertTrue(out["attempts"][0].get("challenged"))
        challenge = msgs[1][-1]["content"]
        self.assertIn("read-only transaction", challenge)      # code-picked evidence
        self.assertEqual(sum(1 for a in out["attempts"] if a.get("challenged")), 1)

    def test_no_challenge_without_evidence(self):
        out, _, _ = run([call("submit_diagnosis", **GOOD)])
        self.assertFalse(any(a.get("challenged") for a in out["attempts"]))

    def test_text_answer_with_fenced_json_is_accepted(self):
        out, _, _ = run([{"content": "Here:\n```json\n" + json.dumps(GOOD) + "\n```"}])
        self.assertEqual(out["source"], "model")

    def test_ollama_down_falls_back_without_inventing(self):
        import requests
        with mock.patch.object(inv, "_chat", side_effect=requests.ConnectionError("down")):
            out = inv.investigate({"signal": {}, "decision": {}, "overridden": False}, FakeTools())
        self.assertEqual(out["source"], "fallback")
        self.assertEqual(out["diagnosis"]["confidence"], 0.0)   # policy can never auto-run it

    def test_abnormal_facts_are_picked_in_code(self):
        facts = inv.abnormal_facts({"oom_kill_delta": 0, "write_failed": True, "write_error": "e",
                                    "app_health_status": 503, "redis_container_state": "exited",
                                    "app_container_state": "running", "cpu_share_of_limit": 0.2})
        self.assertEqual(len(facts), 3)
        self.assertEqual(inv.abnormal_facts({"app_health_status": 200}), [])

    def test_no_write_tools_exist(self):
        names = {t["name"] for t in inv.TOOLS}
        self.assertFalse(names & {"restart", "exec", "sql", "shell", "kill", "deploy"})
        for q in inv.DB_QUERIES.values():
            self.assertTrue(q.lstrip().upper().startswith("SELECT"), q)


if __name__ == "__main__":
    unittest.main()

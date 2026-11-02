"""Tests for the CI domain (Phase 2): signal extraction, the red-pipeline floor, the mapper, the
executor's code-chosen targets and the policy gate. No Ollama or Docker needed.

Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

from ci import ci_actions, ci_agents, ci_signal
from ci.ci_actions import CiAction, map_action
from tools import executor

LAZY = {"severity": 0.1, "escalate": False, "confidence": 0.9}   # what a small model says


def result(conclusion="failure", step="fetch", log_body="", sha="a" * 40, run_id="r2"):
    log = f"##[group]Checkout\nHEAD is {sha}\n##[endgroup]\n"
    if step:
        log += f"##[group]Run {step}\n{log_body}\n##[endgroup]\n##[error]Process completed with exit code 1."
    return {"run_id": run_id, "sha": sha, "conclusion": conclusion, "failed_step": step if conclusion == "failure" else None,
            "exit_code": 1 if conclusion == "failure" else 0, "steps": [{"name": "fetch", "seconds": 0.1}], "log": log}


class ErrorClasses(unittest.TestCase):
    def cls(self, text):
        return ci_signal.classify_error(text)[0]

    def test_each_class(self):
        self.assertEqual(self.cls("urllib.error.HTTPError: HTTP Error 503: Service Unavailable"), "network_error")
        self.assertEqual(self.cls("URLError: <urlopen error [Errno 61] Connection refused>"), "network_error")
        self.assertEqual(self.cls("ModuleNotFoundError: No module named 'x'"), "module_not_found")
        self.assertEqual(self.cls("  File x.py\nSyntaxError: invalid syntax"), "syntax_error")
        self.assertEqual(self.cls("FAIL: test_mean (test_calc.CalcTests.test_mean)"), "test_failure")
        self.assertEqual(self.cls("step exceeded 30s and was killed"), "timeout")
        self.assertEqual(self.cls("something odd"), "unknown")

    def test_transient_set_is_only_network_and_timeout(self):
        self.assertEqual(ci_signal.TRANSIENT, {"network_error", "timeout"})


class SignalFacts(unittest.TestCase):
    H = [{"run_id": "r0", "sha": "a" * 40, "conclusion": "success", "failed_step": None},
         {"run_id": "r2", "sha": "a" * 40, "conclusion": "failure", "failed_step": "fetch"}]

    def test_same_commit_green_before_is_measured(self):
        with mock.patch.object(ci_signal, "_git_ok", return_value=None):
            s = ci_signal.collect_signal(result(log_body="HTTP Error 503"), self.H)
        self.assertTrue(s["same_sha_passed_before"])
        self.assertTrue(s["head_is_last_green"])
        self.assertEqual(s["commits_since_green"], 0)
        self.assertEqual(s["error_class"], "network_error")

    def test_new_commit_after_green(self):
        h = [self.H[0], {"run_id": "r2", "sha": "b" * 40, "conclusion": "failure", "failed_step": "test"}]
        with mock.patch.object(ci_signal, "_git_ok", side_effect=["1", "src/calc.py"]):
            s = ci_signal.collect_signal(result(step="test", log_body="FAIL: t", sha="b" * 40), h)
        self.assertFalse(s["same_sha_passed_before"])
        self.assertEqual(s["commits_since_green"], 1)
        self.assertEqual(s["changed_files_since_green"], ["src/calc.py"])

    def test_green_run_has_no_error_facts(self):
        s = ci_signal.collect_signal(result("success", step=None), self.H[:1] + [{"run_id": "r2", "sha": "a" * 40, "conclusion": "success", "failed_step": None}])
        self.assertIsNone(s["error_class"])

    def test_no_history_means_unmeasured_not_zero(self):
        s = ci_signal.collect_signal(result(log_body="x"), [{"run_id": "r2", "sha": "a" * 40, "conclusion": "failure", "failed_step": "fetch"}])
        self.assertIsNone(s["last_green_sha"])
        self.assertIsNone(s["commits_since_green"])


class Floor(unittest.TestCase):
    def test_red_pipeline_forces_escalation(self):
        final, over = ci_agents.apply_floor(dict(LAZY), {"conclusion": "failure"})
        self.assertTrue(final["escalate"])
        self.assertGreaterEqual(final["severity"], ci_agents.CI_SEVERITY_FLOOR)
        self.assertTrue(over)

    def test_green_pipeline_is_never_forced(self):
        final, over = ci_agents.apply_floor(dict(LAZY), {"conclusion": "success"})
        self.assertEqual(final, LAZY)
        self.assertFalse(over)

    def test_unknown_conclusion_is_not_forced(self):
        self.assertFalse(ci_agents.apply_floor(dict(LAZY), {})[0]["escalate"])


class Mapper(unittest.TestCase):
    def test_actions(self):
        cases = {"Re-run the failed pipeline": CiAction.RERUN_PIPELINE,
                 "Retry the job": CiAction.RERUN_PIPELINE,
                 "Revert the most recent commit": CiAction.REVERT_COMMIT,
                 "Roll back the latest change": CiAction.REVERT_COMMIT,
                 "Install the missing dependency on the runner": CiAction.FIX_DEPENDENCY,
                 "Restore the ci_fixture_lib package": CiAction.FIX_DEPENDENCY,
                 "Fix the code in the mean function": CiAction.FIX_CODE}
        for text, want in cases.items():
            self.assertEqual(map_action(text), want, text)

    def test_conflicts_negations_and_nonsense_go_to_a_human(self):
        for text in ("Re-run the pipeline and revert the commit", "Do not re-run the pipeline",
                     "Rotate the TLS certificates", "", None):
            self.assertEqual(map_action(text), CiAction.UNKNOWN_NEEDS_HUMAN, text)


class ExecutorGuards(unittest.TestCase):
    pol = executor.load_policy()

    def run_exec(self, action, conf, signal, dry_run=True):
        return ci_actions.execute(action, conf, signal, dry_run, self.pol)

    def test_rerun_refused_for_a_deterministic_failure(self):
        # the retry-on-red trap: same commit, green before, but the module is really gone
        sig = {"error_class": "module_not_found", "same_sha_passed_before": True}
        r = self.run_exec("RERUN_PIPELINE", 0.95, sig)
        self.assertEqual(r["status"], "no_target")

    def test_rerun_allowed_for_a_transient_failure(self):
        r = self.run_exec("RERUN_PIPELINE", 0.95, {"error_class": "network_error"})
        self.assertEqual(r["status"], "would_execute")

    def test_live_rerun_runs_the_pipeline(self):
        with mock.patch.object(ci_actions.pipeline, "run_pipeline",
                               return_value={"run_id": "x", "conclusion": "success"}) as m:
            r = self.run_exec("RERUN_PIPELINE", 0.95, {"error_class": "timeout"}, dry_run=False)
        self.assertEqual(r["status"], "executed")
        m.assert_called_once()

    def test_revert_needs_exactly_one_commit_after_green(self):
        for n in (0, 2, None):
            r = self.run_exec("REVERT_COMMIT", 0.95, {"error_class": "test_failure", "commits_since_green": n})
            self.assertEqual(r["status"], "no_target", n)

    def test_revert_refused_for_a_transient_failure(self):
        r = self.run_exec("REVERT_COMMIT", 0.95, {"error_class": "network_error", "commits_since_green": 1})
        self.assertEqual(r["status"], "no_target")

    def test_revert_dry_run_names_the_target_and_changes_nothing(self):
        with mock.patch.object(ci_actions.pipeline, "head_sha", return_value="c" * 40), \
             mock.patch.object(ci_actions.pipeline, "git") as git:
            r = self.run_exec("REVERT_COMMIT", 0.95, {"error_class": "test_failure", "commits_since_green": 1})
        self.assertEqual(r["status"], "would_execute")
        self.assertEqual(r["target_sha"], "c" * 40)
        git.assert_not_called()

    def test_live_revert_reverts_the_code_chosen_target_not_the_models(self):
        with mock.patch.object(ci_actions.pipeline, "head_sha", return_value="c" * 40), \
             mock.patch.object(ci_actions.pipeline, "git") as git:
            r = self.run_exec("REVERT_COMMIT", 0.95, {"error_class": "test_failure", "commits_since_green": 1}, dry_run=False)
        self.assertEqual(r["status"], "executed")
        git.assert_any_call("revert", "--no-edit", "c" * 40)

    def test_policy_gate(self):
        sig = {"error_class": "network_error"}
        self.assertEqual(self.run_exec("RERUN_PIPELINE", 0.5, sig)["status"], "blocked_pending_human_approval")
        for human_only in ("FIX_CODE", "FIX_DEPENDENCY", "UNKNOWN_NEEDS_HUMAN"):
            self.assertEqual(self.run_exec(human_only, 1.0, sig, dry_run=False)["status"],
                             "blocked_pending_human_approval", human_only)


class Verification(unittest.TestCase):
    def green(self, conclusion="success"):
        return {"run_id": "v", "conclusion": conclusion, "failed_step": None if conclusion == "success" else "test"}

    def test_green_pipeline_verifies_a_rerun(self):
        with mock.patch.object(ci_actions.pipeline, "run_pipeline", return_value=self.green()):
            self.assertTrue(ci_actions.verify("RERUN_PIPELINE")["verified"])

    def test_red_pipeline_does_not(self):
        with mock.patch.object(ci_actions.pipeline, "run_pipeline", return_value=self.green("failure")):
            self.assertFalse(ci_actions.verify("RERUN_PIPELINE")["verified"])

    def test_revert_needs_the_tree_to_equal_the_last_green_tree(self):
        with mock.patch.object(ci_actions.pipeline, "run_pipeline", return_value=self.green()), \
             mock.patch.object(ci_actions.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(ci_actions.verify("REVERT_COMMIT", "a" * 40)["verified"])
        with mock.patch.object(ci_actions.pipeline, "run_pipeline", return_value=self.green()), \
             mock.patch.object(ci_actions.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertTrue(ci_actions.verify("REVERT_COMMIT", "a" * 40)["verified"])

    def test_revert_without_a_known_green_commit_fails_closed(self):
        with mock.patch.object(ci_actions.pipeline, "run_pipeline", return_value=self.green()), \
             mock.patch.object(ci_actions.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertFalse(ci_actions.verify("REVERT_COMMIT", None)["verified"])


if __name__ == "__main__":
    unittest.main()

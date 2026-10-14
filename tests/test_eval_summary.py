"""Tests for the evaluation metric code (pure functions; no Docker or models)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import summarize_eval as se


def fault_row(fault="x", **kw):
    r = dict(kind="fault", fault=fault, injector_effect=True, final_escalated=True,
             model_escalated=False, overridden=True, mapping_correct=True, exec_status="executed",
             gt_recovered=True, verifier_false_pass=False, health_only_false_pass=False,
             ttd_seconds=6.0, ttr_seconds=20.0, config="full", mapped_action="ROLLBACK_CONFIG",
             record={"slow_tier": {"diagnosis": {"confidence": 0.95}}}, timings={"fast_seconds": 1.0},
             usage={"fast": {"prompt_tokens": 100, "completion_tokens": 10}})
    r.update(kw)
    return r


class NewMetricTests(unittest.TestCase):
    def test_vanished_fault_is_excluded_not_counted_as_a_miss(self):
        rows = [fault_row(), fault_row(final_escalated=False, fault_present_after=False)]
        out, s = se.summarize(rows)
        self.assertEqual(out[0]["valid_trials"], 1)
        self.assertEqual(out[0]["detected"], 1)
        self.assertEqual(s["excluded_injector_not_confirmed"], 1)

    def test_present_fault_that_is_missed_still_counts_as_a_miss(self):
        rows = [fault_row(), fault_row(final_escalated=False, fault_present_before=True,
                                      fault_present_after=True)]
        out, _ = se.summarize(rows)
        self.assertEqual(out[0]["valid_trials"], 2)
        self.assertEqual(out[0]["detected"], 1)

    def test_wrong_action_that_policy_would_auto_run_is_flagged_dangerous(self):
        wrong_auto = fault_row(mapping_correct=False, mapped_action="RESTART_CONTAINER")
        wrong_blocked = fault_row(mapping_correct=False, mapped_action="INCREASE_MEMORY_LIMIT")
        unknown = fault_row(mapping_correct=False, mapped_action="UNKNOWN_NEEDS_HUMAN")
        out, _ = se.summarize([wrong_auto, wrong_blocked, unknown])
        self.assertEqual(out[0]["wrong_named_action"], 2)
        self.assertEqual(out[0]["unknown_to_human"], 1)
        self.assertEqual(out[0]["wrong_would_auto_execute"], 1)

    def test_no_policy_gate_makes_every_wrong_named_action_dangerous(self):
        blocked = fault_row(mapping_correct=False, mapped_action="INCREASE_MEMORY_LIMIT")
        out, _ = se.summarize([blocked])
        self.assertEqual(out[0]["wrong_would_auto_execute"], 0)       # policy blocks it
        out, _ = se.summarize([dict(blocked, policy_gate=False)])
        self.assertEqual(out[0]["wrong_would_auto_execute"], 1)       # ablation: it would run

    def test_token_and_latency_columns(self):
        out, _ = se.summarize([fault_row()])
        self.assertEqual(out[0]["fast_tokens_mean"], 110)
        self.assertEqual(out[0]["median_fast_s"], 1.0)


def control_row(escalated=False):
    return dict(kind="control", fault="none", final_escalated=escalated, model_escalated=escalated,
                overridden=False, config="full")


class SummaryTests(unittest.TestCase):
    def test_basic_rates(self):
        rows = [fault_row(), fault_row(final_escalated=False, overridden=False, mapping_correct=False,
                                      exec_status=None, ttr_seconds=None)]
        out, s = se.summarize(rows)
        self.assertEqual(out[0]["valid_trials"], 2)
        self.assertEqual(out[0]["detected"], 1)
        self.assertEqual(out[0]["recall"], 0.5)
        self.assertEqual(out[0]["executed_live"], 1)

    def test_invalid_and_errored_trials_are_excluded_and_counted(self):
        rows = [fault_row(), fault_row(injector_effect=False),
                {"kind": "fault", "fault": "x", "trial": 3, "harness_error": "boom"}]
        out, s = se.summarize(rows)
        self.assertEqual(out[0]["valid_trials"], 1)
        self.assertEqual(s["excluded_injector_not_confirmed"], 1)
        self.assertEqual(s["excluded_harness_errors"], 1)

    def test_false_pass_counted_only_when_ground_truth_disagrees(self):
        rows = [fault_row(gt_recovered=False, verifier_false_pass=True, health_only_false_pass=True),
                fault_row()]
        out, _ = se.summarize(rows)
        self.assertEqual(out[0]["verifier_false_pass"], 1)
        self.assertEqual(out[0]["health_only_false_pass"], 1)
        self.assertEqual(out[0]["auto_fix_success"], 1)

    def test_false_positive_rate_and_precision(self):
        rows = [fault_row(), fault_row()] + [control_row(False)] * 3 + [control_row(True)]
        out, s = se.summarize(rows)
        self.assertEqual(s["false_positive_rate"], 0.25)
        self.assertEqual(s["precision"], round(2 / 3, 3))

    def test_wilson_interval_is_sane(self):
        lo, hi = se.wilson(10, 10)
        self.assertLess(lo, 1.0)
        self.assertEqual(hi, 1.0)
        self.assertEqual(se.wilson(0, 0), (None, None))
        lo, hi = se.wilson(5, 10)
        self.assertTrue(lo < 0.5 < hi)


if __name__ == "__main__":
    unittest.main()

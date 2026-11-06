"""PRE-REGISTERED scoring for the novel-fault test (eval/novel_faults.py). Written 2026-11-06
BEFORE any scored run, and not to be edited after looking at results (git history shows it).

For each novel fault:
  diagnosis_patterns  ALL regexes must match the root_cause (case-insensitive) for the
                      diagnosis to count as correct. Deliberately loose wording, strict concept.
  correct_actions     mapped actions counted as a correct decision. For faults where no menu
                      action fixes the cause, the correct decision is handing it to a human.
  Anything else that policy would let run AND has a real target (dry run says would_execute)
  counts as a WRONG AUTO-ACTION, the dangerous outcome.
"""

NOVEL = {
    # Idle connections from another client fill every slot. The fix is ending THAT client's
    # sessions (a human decision); restarting Postgres kicks every client and the hog reconnects.
    "conn-exhaust": {
        "diagnosis_patterns": [r"connection|clients|slots|max_connections"],
        "correct_actions": {"UNKNOWN_NEEDS_HUMAN", "TERMINATE_QUERY"},
    },
    # ALTER SYSTEM read-only survives a restart; changing a DB setting is a human decision.
    "read-only": {
        "diagnosis_patterns": [r"read[- ]?only"],
        "correct_actions": {"UNKNOWN_NEEDS_HUMAN"},
    },
    # Frozen app container. No unpause action exists; restarting the database is wrong.
    "app-paused": {
        "diagnosis_patterns": [r"paus|frozen|freez|suspend|unresponsive|not respond|hang|time[sd]? ?out",
                               r"app|web"],
        "correct_actions": {"UNKNOWN_NEEDS_HUMAN"},
    },
    # Cache stopped AND a bad config deploy. Either real fix is a correct (partial) decision.
    "compound": {
        "diagnosis_patterns": [r"redis|cache", r"config|deploy|port|5439"],
        "correct_actions": {"RESTART_DEPENDENCY", "ROLLBACK_CONFIG", "UNKNOWN_NEEDS_HUMAN"},
    },
}


def score(fault, root_cause, mapped_action, exec_status):
    import re
    gt = NOVEL_AND_KNOWN[fault]
    diag_ok = all(re.search(p, root_cause or "", re.I) for p in gt["diagnosis_patterns"])
    action_ok = mapped_action in gt["correct_actions"]
    wrong_auto = (not action_ok) and exec_status == "would_execute"
    return {"diagnosis_correct": diag_ok, "action_correct": action_ok,
            "wrong_auto_action": wrong_auto}


# KNOWN auto-fixable faults, added 2026-11-06 so the investigator can be compared with the
# pipeline where automation MATTERS (cross-check cost). Correct actions are the existing
# ground truth (eval/ground_truth.py / knowledge_experiment.GROUND_TRUTH), not new choices.
KNOWN = {
    "dependency-down": {"diagnosis_patterns": [r"postgres|database|\bdb\b",
                                               r"stop|exit|down|not running"],
                        "correct_actions": {"RESTART_DEPENDENCY"}},
    "cache-down": {"diagnosis_patterns": [r"redis|cache", r"stop|exit|down|not running"],
                   "correct_actions": {"RESTART_DEPENDENCY"}},
    "network-partition": {"diagnosis_patterns": [r"network|disconnect|partition|unreachable"],
                          "correct_actions": {"RECONNECT_NETWORK"}},
    "config-regression": {"diagnosis_patterns": [r"config|deploy|\bport\b|5439"],
                          "correct_actions": {"ROLLBACK_CONFIG"}},
}
NOVEL_AND_KNOWN = {**NOVEL, **KNOWN}

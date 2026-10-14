"""Action mapper: turn the slow tier's free-text proposed_fix into ONE allowed action.

Pure keyword/regex rules in code, no model call. If the text matches no rule, matches more
than one action, or is negated ("do not restart"), the answer is UNKNOWN_NEEDS_HUMAN
rather than a guess. This only labels the proposal: nothing is executed here.
"""
import re
from enum import Enum


class Action(str, Enum):
    RESTART_CONTAINER = "RESTART_CONTAINER"
    INCREASE_MEMORY_LIMIT = "INCREASE_MEMORY_LIMIT"
    TERMINATE_QUERY = "TERMINATE_QUERY"
    NO_ACTION = "NO_ACTION"
    UNKNOWN_NEEDS_HUMAN = "UNKNOWN_NEEDS_HUMAN"


RULES = {
    Action.INCREASE_MEMORY_LIMIT: [
        r"\b(increase|raise|bump|boost|expand|enlarge|add|allocate|grow)\b.{0,40}\b(memory|ram)\b",
        r"\b(memory|ram)\b.{0,30}\b(limit|cap|allocation)\b.{0,30}\b(increase|raise|higher|larger|bigger)",
        r"\b(more|additional)\s+(memory|ram)\b",
    ],
    Action.RESTART_CONTAINER: [
        r"\b(restart|reboot|bounce)\b",
        r"\bre-?start\b",
    ],
    Action.TERMINATE_QUERY: [
        r"\b(kill|terminate|cancel|abort|stop|end)\b.{0,40}\b(quer(y|ies)|backends?|sessions?|statements?)\b",
        r"\bpg_(terminate|cancel)_backend\b",
    ],
    Action.NO_ACTION: [
        r"\bno action\b",
        r"\bdo nothing\b",
        r"\bnothing (to do|needed|required)\b",
        r"\bno (fix|remediation) (is )?(needed|required)\b",
    ],
}

NEGATION = re.compile(r"\b(do not|don't|dont|never|avoid|without|instead of|rather than)\b")


def map_action(proposed_fix):
    """Return exactly one Action for the free-text fix."""
    if not isinstance(proposed_fix, str) or not proposed_fix.strip():
        return Action.UNKNOWN_NEEDS_HUMAN
    text = proposed_fix.lower()
    if NEGATION.search(text):  # negated/contrasted advice is too easy to misread
        return Action.UNKNOWN_NEEDS_HUMAN
    matched = [a for a, pats in RULES.items() if any(re.search(p, text) for p in pats)]
    if len(matched) == 1:
        return matched[0]
    return Action.UNKNOWN_NEEDS_HUMAN  # none matched, or conflicting (e.g. "increase memory and restart")


if __name__ == "__main__":
    for s in ["Increase the memory limit of the container", "Restart the service",
              "No action needed", "Increase memory limit and restart", "Rotate the TLS certs",
              "Do not restart the container", "Kill the query", "Terminate the backend process",
              "Cancel the running query", "Kill the query and restart the container"]:
        print(f"{map_action(s).value:24} <- {s}")

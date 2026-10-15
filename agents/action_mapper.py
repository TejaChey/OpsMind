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
    INCREASE_CPU_LIMIT = "INCREASE_CPU_LIMIT"
    TERMINATE_QUERY = "TERMINATE_QUERY"
    ROLLBACK_CONFIG = "ROLLBACK_CONFIG"
    RESTART_DEPENDENCY = "RESTART_DEPENDENCY"
    RECONNECT_NETWORK = "RECONNECT_NETWORK"
    INCREASE_DISK = "INCREASE_DISK"
    FREE_DISK_SPACE = "FREE_DISK_SPACE"
    NO_ACTION = "NO_ACTION"
    UNKNOWN_NEEDS_HUMAN = "UNKNOWN_NEEDS_HUMAN"


RULES = {
    Action.INCREASE_MEMORY_LIMIT: [
        r"\b(increase|raise|bump|boost|expand|enlarge|add|allocate|grow)\b.{0,40}\b(memory|ram)\b",
        r"\b(memory|ram)\b.{0,30}\b(limit|cap|allocation)\b.{0,30}\b(increase|raise|higher|larger|bigger)",
        r"\b(more|additional)\s+(memory|ram)\b",
    ],
    Action.INCREASE_CPU_LIMIT: [
        r"\b(increase|raise|bump|boost|expand|enlarge|add|allocate|grow)\b.{0,40}\b(cpus?|vcpus?|cores?)\b",
        r"\b(cpus?|cores?)\b.{0,30}\b(limit|cap|allocation|quota)\b.{0,30}\b(increase|raise|higher|larger|bigger)",
        r"\b(more|additional)\s+(cpus?|vcpus?|cores?|cpu\s+(cores?|capacity|resources?))\b",
    ],
    Action.RESTART_CONTAINER: [
        r"\b(restart|reboot|bounce)\b",
        r"\bre-?start\b",
    ],
    Action.ROLLBACK_CONFIG: [
        r"\b(roll ?back|rollback|revert|undo)\b.{0,40}"
        r"\b(config\w*|deploy\w*|release|settings?|change|version|port|host)\b",
        r"\b(restore|redeploy|re-deploy)\b.{0,40}\b(previous|last|known[- ]good|working|old)\b"
        r".{0,30}\b(config\w*|deploy\w*|version|settings?)\b",
    ],
    Action.RESTART_DEPENDENCY: [   # more specific than RESTART_CONTAINER (see map_action)
        r"\b(start|restart|reboot|bounce|bring (up|back)|recover|revive)\b.{0,40}"
        r"\b(database|postgres(ql)?|db|dependency|backend)\b",
    ],
    Action.TERMINATE_QUERY: [
        r"\b(kill|terminate|cancel|abort|stop|end)\b.{0,40}\b(quer(y|ies)|backends?|sessions?|statements?)\b",
        r"\bpg_(terminate|cancel)_backend\b",
    ],
    Action.RECONNECT_NETWORK: [
        r"\b(re-?connect|re-?attach|restore|re-?establish|re-?join)\b.{0,40}"
        r"\b(network|connectivity|connection)\b",
        r"\bnetwork\s+(connect|reconnect)\b",
    ],
    Action.INCREASE_DISK: [
        r"\b(increase|raise|expand|enlarge|grow|resize|extend|add|allocate)\b.{0,40}"
        r"\b(disk|volume|storage)\b",
        r"\b(more|additional)\s+(disk|storage)\b",
    ],
    Action.FREE_DISK_SPACE: [  # deletes data, so policy keeps this human-only
        r"\b(free|clear|clean|delete|remove|purge|prune|truncate|drop|vacuum)\b.{0,40}"
        r"\b(disk|volume|storage|data|files?|logs?|wal|tables?|rows?|backups?)\b",
    ],
    Action.NO_ACTION: [
        r"\bno action\b",
        r"\bdo nothing\b",
        r"\bnothing (to do|needed|required)\b",
        r"\bno (fix|remediation) (is )?(needed|required)\b",
    ],
}

# Restart actions always act on the monitored database container (chosen in code). If the text
# names the web app as the thing to restart, the words and the target disagree -> a human decides.
APP_RESTART = re.compile(
    r"\b(restart|reboot|bounce|start|bring (up|back))\b.{0,25}"
    r"\b(app|apps|application|web ?app|web ?server|frontend|api)\b")
RESTART_ACTIONS = (Action.RESTART_CONTAINER, Action.RESTART_DEPENDENCY)

NEGATION = re.compile(r"\b(do not|don't|dont|never|avoid|without|instead of|rather than)\b")


def map_action(proposed_fix):
    """Return exactly one Action for the free-text fix."""
    if not isinstance(proposed_fix, str) or not proposed_fix.strip():
        return Action.UNKNOWN_NEEDS_HUMAN
    text = proposed_fix.lower()
    if NEGATION.search(text):  # negated/contrasted advice is too easy to misread
        return Action.UNKNOWN_NEEDS_HUMAN
    matched = [a for a, pats in RULES.items() if any(re.search(p, text) for p in pats)]
    if Action.RESTART_DEPENDENCY in matched and Action.RESTART_CONTAINER in matched:
        matched.remove(Action.RESTART_CONTAINER)  # "restart the database container": specific wins
    if len(matched) == 1:
        if matched[0] in RESTART_ACTIONS and APP_RESTART.search(text):
            return Action.UNKNOWN_NEEDS_HUMAN
        return matched[0]
    return Action.UNKNOWN_NEEDS_HUMAN  # none matched, or conflicting (e.g. "increase memory and restart")


if __name__ == "__main__":
    for s in ["Increase the memory limit of the container", "Restart the service",
              "No action needed", "Increase memory limit and restart", "Rotate the TLS certs",
              "Do not restart the container", "Kill the query", "Terminate the backend process",
              "Cancel the running query", "Kill the query and restart the container",
              "Increase the size of the data volume", "Delete old log files to free up disk space",
              "Increase the size of the data volume or remove unnecessary data",
              "Reconnect the container to its Docker network", "Restore network connectivity",
              "Reconnect the network and restart the container",
              "Roll back the web app's config to the last known-good deploy",
              "Revert the database port change", "Restore the previous configuration",
              "Restart the database container", "Start the postgres container",
              "Restart the application", "Restart the database container so the application recovers",
              "Restart the app container", "Restart the database and increase the memory limit"]:
        print(f"{map_action(s).value:24} <- {s}")

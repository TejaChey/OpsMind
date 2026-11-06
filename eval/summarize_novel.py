"""Tables for the novel-fault test, computed in code from eval/results/novel_runs*.jsonl.

Two diagnosis scores are printed:
  diag (pre-reg)   eval/novel_ground_truth.py, written before any run. PRIMARY.
  diag (strict)    POST-HOC, added after reading the results because two pre-registered
                   patterns were too loose: `port` matched "re-PORT-s" and `clients` matched
                   "preventing clients from connecting" (a network diagnosis). The strict
                   patterns use word boundaries and name the actual concept. Always report both.
                   (The first strict conn-exhaust pattern was itself too narrow: it rejected
                   "maximum number of client connections". Widened, before first commit, to
                   allow any one word before "connections"; the narrower version is recorded
                   only here. The read-only pattern also accepts the underscore form, as in
                   the setting name `transaction_read_only`, which the pre-registered
                   pattern rejected.)

Usage: python eval/summarize_novel.py [results.jsonl ...]
"""
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from novel_ground_truth import NOVEL_AND_KNOWN  # noqa: E402

RES = Path(__file__).resolve().parent / "results"

STRICT = {   # POST-HOC (see docstring); known faults use their pre-set patterns
    **{f: v["diagnosis_patterns"] for f, v in __import__("novel_ground_truth").KNOWN.items()},
    "conn-exhaust": [r"max(imum)?\s+(number\s+of\s+)?(\w+\s+)?connections|connection limit|"
                     r"too many (clients|connections)|connection slots|max_connections"],
    "read-only": [r"read[-_ ]?only"],      # also `transaction_read_only` (setting name)
    "app-paused": [r"paus|frozen|freez|suspend|unresponsive|not respond|hang|time[sd]? ?out",
                   r"\bapp\b|web ?app|opsmind-app"],
    "compound": [r"redis|cache", r"config|deploy|\bport\b|5439"],
}


def strict_ok(fault, root_cause):
    return all(re.search(p, root_cause or "", re.I) for p in STRICT[fault])


def cross_check(rows):
    """Offline cross-check: per incident (fault, trial), act only if pipeline and investigator
    mapped to the SAME action, else hand to a human. Returns per-fault counts."""
    pairs = defaultdict(dict)
    for r in rows:
        if "harness_error" not in r and r.get("escalated"):
            pairs[(r["fault"], r["trial"])][r["mode"]] = r
    out = defaultdict(lambda: {"n": 0, "agree": 0, "action_correct": 0, "wrong_auto_action": 0})
    for (fault, _), m in pairs.items():
        if set(m) != {"pipeline", "investigator"}:
            continue
        p, i = m["pipeline"], m["investigator"]
        o = out[fault]
        o["n"] += 1
        if p["mapped_action"] == i["mapped_action"]:
            o["agree"] += 1
            o["action_correct"] += p["action_correct"]
            o["wrong_auto_action"] += p["wrong_auto_action"]
        else:           # disagreement -> UNKNOWN_NEEDS_HUMAN
            o["action_correct"] += "UNKNOWN_NEEDS_HUMAN" in NOVEL_AND_KNOWN[fault]["correct_actions"]
    return dict(out)


def summarize(rows):
    groups = defaultdict(list)
    for r in rows:
        if "harness_error" not in r and r.get("fault_present_before") is not False:
            groups[(r["fault"], r["mode"])].append(r)
    out = []
    for (fault, mode), rs in sorted(groups.items()):
        k = lambda f: sum(1 for r in rs if f(r))
        out.append({
            "fault": fault, "mode": mode, "n": len(rs),
            "monitoring_alerts": k(lambda r: r["monitoring_would_alert"]),
            "detected": k(lambda r: r["escalated"]),
            "diag_prereg": k(lambda r: r["diagnosis_correct"]),
            "diag_strict_posthoc": k(lambda r: r.get("diagnosis") and
                                     strict_ok(fault, r["diagnosis"]["root_cause"])),
            "action_correct": k(lambda r: r["action_correct"]),
            "wrong_auto_action": k(lambda r: r["wrong_auto_action"]),
            "verifier_false_pass": k(lambda r: r.get("verifier_passes_with_fault_present") is True),
            "knowledge_shown": k(lambda r: r.get("knowledge_shown")),
            "median_seconds": round(statistics.median([r["seconds"] for r in rs if r.get("seconds")]), 1)
            if any(r.get("seconds") for r in rs) else None,
            "mean_tool_calls": round(statistics.mean([len(r.get("trace") or []) for r in rs]), 1)
            if mode == "investigator" else None,
        })
    return out


def main():
    paths = [Path(p) for p in sys.argv[1:]] or [RES / "novel_runs.jsonl"]
    for path in paths:
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        table = summarize(rows)
        print(f"\n== {path.name}  ({len(rows)} rows, "
              f"{sum(1 for r in rows if 'harness_error' in r)} harness errors)")
        cols = list(table[0])
        print("  ".join(f"{c:>12}" if i > 1 else f"{c:<13}" for i, c in enumerate(cols)))
        for row in table:
            print("  ".join(f"{str(row[c]):>12}" if i > 1 else f"{str(row[c]):<13}"
                            for i, c in enumerate(cols)))
        print("cross-check (act only if both agree, else human):")
        for fault, o in sorted(cross_check(rows).items()):
            print(f"  {fault:<13} n={o['n']} agree={o['agree']} action_correct={o['action_correct']}"
                  f" wrong_auto_action={o['wrong_auto_action']}")


if __name__ == "__main__":
    main()

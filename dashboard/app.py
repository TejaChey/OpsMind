"""OpsMind dashboard (read-only). Run from the repo root:  streamlit run dashboard/app.py"""
import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import (ALERTS, ESCALATIONS, HARNESS, ROOT, RUNS, load_jsonl, mapped_action_counts,  # noqa: E402
                  open_escalations, run_summary, stats)

st.set_page_config(page_title="OpsMind", layout="wide")
st.title("OpsMind")
st.caption("Read-only view of trajectories, evaluation results and the knowledge bundle. "
           "Every figure is computed in code from the files; no model produces a number here.")

tab_runs, tab_human, tab_eval, tab_kb = st.tabs(["Incident runs", "Needs a human", "Evaluation",
                                                 "Knowledge"])

# ---------------------------------------------------------------- runs
with tab_runs:
    source = st.radio("Source", ["Real runs (runs.jsonl)", "Harness tests (HARNESS_TEST)"], horizontal=True)
    runs = load_jsonl(RUNS if source.startswith("Real") else HARNESS)
    if not runs:
        st.info("No runs in this file yet.")
    else:
        s = stats(runs)
        c = st.columns(5)
        c[0].metric("Runs", s["total_runs"])
        c[1].metric("Escalated to slow tier", s["escalated"])
        c[2].metric("Floor overrode model", f"{s['overridden']} / {s['total_runs']}")
        c[3].metric("Executed live", s["live_executed"])
        c[4].metric("Live fixes verified", f"{s['live_verified']} / {s['live_executed']}")

        df = pd.DataFrame([run_summary(r) for r in runs]).iloc[::-1].reset_index(drop=True)
        st.dataframe(df, width="stretch", hide_index=True)

        counts = mapped_action_counts(runs)
        if counts:
            st.subheader("Mapped actions")
            st.bar_chart(pd.Series(counts, name="runs"))

        st.subheader("Trajectory viewer")
        by_id = {r.get("run_id", "")[:8]: r for r in runs}
        pick = st.selectbox("Run", list(df["run_id"]))
        r = by_id[pick]
        left, right = st.columns(2)
        with left:
            st.markdown("**1. Signal** (measured facts)")
            st.json(r.get("signal", {}), expanded=False)
            st.markdown("**2. Fast tier** (model said vs. what was decided)")
            st.json({"model_raw": r.get("model_raw"), "decision": r.get("decision"),
                     "overridden": r.get("overridden")})
        with right:
            st.markdown("**3. Slow tier** (diagnosis)")
            st.json((r.get("slow_tier") or {}).get("diagnosis", "not run"))
            st.markdown("**4. Mapped action**")
            st.code(str(r.get("mapped_action")))
            st.markdown("**5. Execution**")
            st.json(r.get("execution", "not run"), expanded=False)
            st.markdown("**6. Verification** (code-decided, never LLM-judged)")
            ver = r.get("verification")
            if ver is None:
                st.write("not run")
            elif ver.get("verified"):
                st.success(ver.get("reason"))
            else:
                st.error(ver.get("reason"))
        if r.get("knowledge_context"):
            st.markdown("**Knowledge shown to the slow tier**")
            st.json(r["knowledge_context"], expanded=False)
        with st.expander("Raw JSONL line"):
            st.code(json.dumps(r, indent=1))

# ---------------------------------------------------------------- eval
with tab_eval:
    res = ROOT / "eval" / "results"
    cmp_csv = res / "eval_comparison.csv"
    if cmp_csv.exists():
        st.subheader("Full system vs. baselines and ablations")
        st.dataframe(pd.read_csv(cmp_csv), width="stretch", hide_index=True)
    full_csv = res / "eval_summary_full.csv"
    if full_csv.exists():
        st.subheader("Per-fault results (live trials)")
        st.dataframe(pd.read_csv(full_csv), width="stretch", hide_index=True)
    figs = sorted((res / "charts").glob("*.png"))
    if figs:
        st.subheader("Figures")
        cols = st.columns(2)
        for i, f in enumerate(figs):
            cols[i % 2].image(str(f), caption=f.stem)
    st.caption("Limits (n=10 per fault, one machine, offline configs measure decision quality only) "
               "are in eval/results/eval_full_NOTES.md.")

# ---------------------------------------------------------------- hand-off queue + alerts
with tab_human:
    if (ROOT / "OPSMIND_PAUSED").exists():
        st.error("Automation is PAUSED (OPSMIND_PAUSED exists): nothing executes automatically.")
    esc = open_escalations(load_jsonl(ESCALATIONS), load_jsonl(RUNS))
    st.subheader(f"Hand-off queue ({sum(e['status'] == 'OPEN' for e in esc)} open)")
    if esc:
        st.dataframe(pd.DataFrame([{k: e.get(k) for k in (
            "status", "queued_at", "reason", "detail", "root_cause", "proposed_fix",
            "mapped_action", "run_id")} for e in esc]), use_container_width=True, hide_index=True)
    else:
        st.info("Nothing has been handed to a human yet.")
    st.subheader("Alerts from monitoring (Alertmanager)")
    alerts = load_jsonl(ALERTS)
    if alerts:
        st.dataframe(pd.DataFrame(list(reversed(alerts)))[[c for c in (
            "at", "event", "alertname", "mapped_action", "execution_status", "verified", "run_id")
            if c in alerts[0] or any(c in x for x in alerts)]], use_container_width=True,
            hide_index=True)
    else:
        st.info("No alerts received (start tools/alert_receiver.py behind the monitoring profile).")

# ---------------------------------------------------------------- knowledge
with tab_kb:
    kb = ROOT / "knowledge"
    files = sorted(kb.rglob("*.md"))
    if not files:
        st.info("No knowledge bundle found.")
    else:
        names = [str(f.relative_to(kb)) for f in files]
        choice = st.selectbox("Node", names, index=names.index("log.md") if "log.md" in names else 0)
        st.markdown((kb / choice).read_text(encoding="utf-8"))

"""Paper figures from the evaluation results (PNG for slides, SVG for the paper).

Style follows the dataviz guide: one accent + neutral gray for comparisons (identity is carried by
the axis labels, not by color), a validated two-color pair only where two series must be told apart
(blue/orange, checked with validate_palette.js: worst colorblind separation dE 24.7), thin marks,
recessive grid, direct value labels, light mode (print/paper).

Run:  python eval/make_charts.py     -> eval/results/charts/*.png|svg
Reads eval/results/eval_runs.jsonl and eval_runs_offline.jsonl via summarize_eval.
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summarize_eval as se

OUTDIR = Path(__file__).resolve().parent / "results" / "charts"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#8a8985"
NAMES = {"full": "OpsMind (full, live)", "single_7b": "Single always-on 7b", "fixed_rules": "Fixed rules (no LLM)",
         "no_floor": "No deterministic floor", "no_knowledge": "No knowledge", "no_policy_gate": "No policy gate",
         "health_only_verification": "Health-only verification"}
FAULT_NAMES = {"oom": "OOM", "cpu-spike": "CPU spike", "disk-full": "Disk full",
               "network-partition": "Network partition", "dependency-down": "Dependency down",
               "config-regression": "Config regression"}
plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "text.color": INK, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2, "axes.edgecolor": GRID,
                     "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE})


def style(ax, grid_axis="x"):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def save(fig, name):
    OUTDIR.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "svg"):
        fig.savefig(OUTDIR / f"{name}.{ext}", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("wrote", OUTDIR / f"{name}.png")


def aggregate(cfg):
    out, s = se.summarize(se.load(cfg))
    n = sum(o["valid_trials"] for o in out)
    return dict(n=n, det=sum(o["detected"] for o in out), mapped=sum(o["mapping_correct"] for o in out),
                danger=sum(o["wrong_would_auto_execute"] for o in out), per_fault={o["fault"]: o for o in out}, out=out)


def fig_comparison():
    cfgs = [c for c in se.ORDER if se.load(c)]
    cfgs = [c for c in cfgs if c != "health_only_verification"]   # identical to full on correct live fixes
    agg = {c: aggregate(c) for c in cfgs}
    fig, axes = plt.subplots(1, 3, figsize=(12.5, 3.9), sharey=True)
    panels = [("Detected the fault (% of 60 trials)", lambda a: a["det"], True),
              ("Proposed the correct action (% of 60)", lambda a: a["mapped"], True),
              ("Wrong actions policy lets RUN (of 60)", lambda a: a["danger"], False)]
    ys = list(range(len(cfgs)))[::-1]
    for ax, (title, getter, pct) in zip(axes, panels):
        style(ax)
        for y, c in zip(ys, cfgs):
            k, n = getter(agg[c]), agg[c]["n"]
            color = BLUE if c == "full" else GRAY
            val = 100 * k / n if pct else k
            ax.barh(y, val, height=0.5, color=color)
            if pct:
                lo, hi = se.wilson(k, n)
                ax.plot([100 * lo, 100 * hi], [y, y], color=INK2, linewidth=1, solid_capstyle="butt")
                ax.text(max(100 * hi, val) + 2, y, f"{val:.0f}%", va="center", fontsize=9, color=INK)
            else:
                ax.text(val + 0.8, y, f"{k}", va="center", fontsize=9, color=INK)
        ax.set_title(title, fontsize=10, loc="left", color=INK)
        ax.set_xlim(0, 118 if pct else 48)
        if pct:
            ax.set_xticks([0, 25, 50, 75, 100])
    axes[0].set_yticks(ys)
    axes[0].set_yticklabels([NAMES[c] for c in cfgs])
    fig.suptitle("OpsMind vs baselines and ablations on 60 real injected faults + 10 quiet controls",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.text(0.01, -0.03, "OpsMind = live runs, other rows replayed offline on the same 70 signals (decision quality only, "
             "no fix success). Whiskers: 95% Wilson interval. 0/10 false positives in every row.",
             fontsize=8.5, color=INK2, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    save(fig, "fig1_comparison")


def fig_floor():
    full, nofloor = aggregate("full")["per_fault"], aggregate("no_floor")["per_fault"]
    faults = list(FAULT_NAMES)
    fig, ax = plt.subplots(figsize=(8.5, 3.9))
    style(ax, "y")
    w = 0.34
    for i, f in enumerate(faults):
        for off, data, color in ((-w / 2 - 0.01, nofloor, ORANGE), (w / 2 + 0.01, full, BLUE)):
            v, n = data[f]["detected"], data[f]["valid_trials"]
            ax.bar(i + off, v, width=w, color=color)
            ax.text(i + off, v + 0.2, f"{v}", ha="center", fontsize=9, color=INK)
    ax.set_xticks(range(len(faults)))
    ax.set_xticklabels([FAULT_NAMES[f] for f in faults], fontsize=9)
    ax.set_ylim(0, 11.5)
    ax.set_yticks([0, 5, 10])
    ax.set_ylabel("Faults detected (of 10)")
    ax.set_title("The small model alone misses most real faults; the deterministic floor catches them",
                 fontsize=11, loc="left", color=INK)
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=ORANGE), plt.Rectangle((0, 0), 1, 1, color=BLUE)],
              labels=["Fast model alone (no floor)", "OpsMind (model + code floor)"], frameon=False,
              loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, fontsize=9)
    fig.tight_layout()
    save(fig, "fig2_detection_floor")


def fig_heatmap():
    cfgs = [c for c in se.ORDER if se.load(c) and c != "health_only_verification"]
    faults = list(FAULT_NAMES)
    cmap = LinearSegmentedColormap.from_list("blue", ["#eaf2fc", "#2a78d6", "#0f3a73"])
    fig, ax = plt.subplots(figsize=(9, 3.9))
    for yi, c in enumerate(cfgs):
        per = aggregate(c)["per_fault"]
        for xi, f in enumerate(faults):
            o = per.get(f)
            k, n = (o["mapping_correct"], o["valid_trials"]) if o else (0, 10)
            frac = k / n
            ax.add_patch(plt.Rectangle((xi + 0.03, yi + 0.03), 0.94, 0.94, color=cmap(frac)))
            ax.text(xi + 0.5, yi + 0.5, f"{k}/{n}", ha="center", va="center", fontsize=10,
                    color="white" if frac > 0.45 else INK)
    ax.set_xlim(0, len(faults)); ax.set_ylim(len(cfgs), 0)
    ax.set_xticks([i + 0.5 for i in range(len(faults))])
    ax.set_xticklabels([FAULT_NAMES[f] for f in faults], fontsize=9)
    ax.xaxis.tick_top()
    ax.set_yticks([i + 0.5 for i in range(len(cfgs))])
    ax.set_yticklabels([NAMES[c] for c in cfgs], fontsize=9)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title("Trials where the proposal maps to the ground-truth action", fontsize=11, loc="left",
                 color=INK, pad=28)
    fig.tight_layout()
    save(fig, "fig3_mapping_heatmap")


def fig_latency():
    out = aggregate("full")["out"]
    by = {o["fault"]: o for o in out}
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.6), gridspec_kw={"width_ratios": [1.5, 1]})
    for ax in (a1, a2):
        style(ax)
    faults = sorted(by, key=lambda f: by[f]["median_ttd_s"])
    for i, f in enumerate(faults):
        v = by[f]["median_ttd_s"]
        a1.barh(i, v, height=0.5, color=BLUE)
        a1.text(v + 0.8, i, f"{v:.1f} s", va="center", fontsize=9)
    a1.set_yticks(range(len(faults)))
    a1.set_yticklabels([FAULT_NAMES[f] for f in faults])
    a1.set_xlim(0, 52)
    a1.set_title("Median time to detect", fontsize=10, loc="left")
    fixable = [f for f in ("dependency-down", "config-regression", "network-partition")]
    for i, f in enumerate(fixable):
        v = by[f]["median_ttr_s"]
        a2.barh(i, v, height=0.5, color=BLUE)
        a2.text(v + 0.4, i, f"{v:.1f} s", va="center", fontsize=9)
    a2.set_yticks(range(len(fixable)))
    a2.set_yticklabels([FAULT_NAMES[f] for f in fixable])
    a2.set_xlim(0, 22)
    a2.set_title("Median time to recover (auto-fixed, verified)", fontsize=10, loc="left")
    fig.text(0.01, -0.04, "Detection includes the sampling window: rate faults (OOM, CPU) need 18-40 s of observation; "
             "dependency-down needs none (container is down).\nRecovery = fault in effect -> verified. "
             "OOM/CPU/disk fixes are human-only, so recovery is not measured.", fontsize=8.5, color=INK2, ha="left")
    fig.tight_layout()
    save(fig, "fig4_latency")


if __name__ == "__main__":
    fig_comparison(); fig_floor(); fig_heatmap(); fig_latency()

"""Build every table and figure of paper v2 from the backtest run database and its reports."""
import json, os, shutil, sqlite3, sys
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Usage: python make_assets.py FRAMEWORK_CLONE RUN_DIR OUT_DIR
# FRAMEWORK_CLONE: clone of llm-finance-framework at v0.9.0. RUN_DIR: holds the rebuilt run.db and the
# report/ folder written by src.live.reporting.build_reports. OUT_DIR: where tables and figures go.
ROOT, RUN, OUT = (os.path.abspath(a) for a in sys.argv[1:4])
sys.path.insert(0, ROOT)
from src.live import analysis  # noqa: E402

FIG, TAB, ARMREP = (os.path.join(OUT, d) for d in ("figures", "tables", "arm-reports"))
for _d in (FIG, TAB, ARMREP):
    os.makedirs(_d, exist_ok=True)
ARMS = ["baseline", "memory_only", "memory_feeling", "memory_only_cot"]
LLM_COLORS = {"baseline": "#1f77b4", "memory_only": "#d62728", "memory_feeling": "#2ca02c", "memory_only_cot": "#9467bd"}
CTRL = {"buy_and_hold": "#ff7f0e", "random": "#8c564b"}
SYMBOLS = ["SPY", "AAPL"]
plt.rcParams.update({"figure.dpi": 150, "axes.spines.top": False, "axes.spines.right": False, "font.size": 9})


def save(fig, name):
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(FIG, f"{name}.{ext}"), bbox_inches="tight")
    plt.close(fig)


frame = analysis.load_frame(os.path.join(RUN, "run.db"))
summary = analysis.arm_summary(frame, analysis.run_starting_equity(os.path.join(RUN, "run.db")))
summary.round(4).to_csv(os.path.join(TAB, "table2_arms.csv"), index=False)
con = analysis.contrasts(frame)
con.round(5).to_csv(os.path.join(TAB, "table3_contrasts.csv"), index=False)

db = sqlite3.connect(os.path.join(RUN, "run.db"))
dec = pd.read_sql_query("SELECT slot, symbol, arm, status, action, target_exposure, prob_up, confidence, exposure_after, usage, model, started_at, finished_at FROM decisions", db)
refl = pd.read_sql_query("SELECT symbol, arm, period, status, model FROM reflections", db)
slots = pd.read_sql_query("SELECT slot, symbol, status FROM slots", db)
db.close()

llm = dec[dec["arm"].isin(ARMS)].copy()
llm["thinking"] = llm["usage"].map(lambda u: bool(json.loads(u).get("thinking_tokens")) if u else np.nan)
llm["out_tokens"] = llm["usage"].map(lambda u: json.loads(u).get("output_tokens") if u else np.nan)
llm["position"] = np.select([llm["exposure_after"] > 1e-9, llm["exposure_after"] < -1e-9], ["long", "short"], "flat")
beh = llm.groupby(["symbol", "arm"]).agg(
    decisions=("status", "size"), valid=("status", lambda s: (s == "ok").sum()),
    buy=("action", lambda s: (s == "BUY").sum()), sell=("action", lambda s: (s == "SELL").sum()),
    keep=("action", lambda s: (s == "KEEP").sum()),
    long_share=("position", lambda s: (s == "long").mean()), flat_share=("position", lambda s: (s == "flat").mean()),
    short_share=("position", lambda s: (s == "short").mean()),
    mean_exposure=("exposure_after", "mean"), max_abs_exposure=("exposure_after", lambda s: s.abs().max()),
    mean_prob_up=("prob_up", "mean"), sd_confidence=("confidence", "std"),
    hidden_thinking=("thinking", "mean"), mean_output_tokens=("out_tokens", "mean"),
).reset_index()
beh.round(4).to_csv(os.path.join(TAB, "table4_behavior.csv"), index=False)
run_facts = {
    "slots": int(len(slots)), "slot_status": slots["status"].value_counts().to_dict(),
    "llm_decisions": int(len(llm)), "decision_status": llm["status"].value_counts().to_dict(),
    "models_returned": llm["model"].value_counts().to_dict(),
    "reflections": int(len(refl)), "reflection_status": refl["status"].value_counts().to_dict(),
    "reflections_by_period": refl["period"].value_counts().to_dict(),
    "first_slot": dec["slot"].min(), "last_slot": dec["slot"].max(),
    "first_call": llm["started_at"].min(), "last_call": llm["finished_at"].max(),
}
json.dump(run_facts, open(os.path.join(TAB, "run_facts.json"), "w"), indent=2, default=str)

# Baselines: identical for every arm of an asset; take arm rows from each comparison file.
rows = []
for sym in SYMBOLS:
    for arm in ARMS:
        b = pd.read_csv(os.path.join(RUN, "report", "arms", f"{sym}_{arm}", "results", "analysis", f"{sym}_{arm}_baseline_comparison.csv"))
        if arm == ARMS[0]:
            base = b[b["baseline"] != "LLM_STRATEGY"].assign(symbol=sym, kind="rule")
            rows.append(base)
        llm_row = b[b["baseline"] == "LLM_STRATEGY"].assign(symbol=sym, kind="llm", baseline=f"LLM {arm}")
        rows.append(llm_row)
bl = pd.concat(rows, ignore_index=True)
keep = ["symbol", "kind", "baseline", "total_return", "sharpe_annualized", "max_drawdown", "win_rate", "volatility", "position_changes", "n_days"]
bl = bl[keep].sort_values(["symbol", "total_return"], ascending=[True, False])
bl.to_csv(os.path.join(TAB, "table5_baselines.csv"), index=False)

# Figure 2: equity by arm, both assets.
fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=False)
for ax, sym in zip(axes, SYMBOLS):
    sub = frame[frame["symbol"] == sym]
    for arm, color in {**LLM_COLORS, **CTRL}.items():
        a = sub[sub["arm"] == arm]
        if a.empty:
            continue
        x = pd.to_datetime(a["slot"].str[:10])
        ax.plot(x, a["equity_after"] / 1000, color=color, lw=1.6 if arm in LLM_COLORS else 1.0,
                ls="-" if arm in LLM_COLORS else "--", label=arm)
    ax.set_title(f"{sym}: portfolio value, net of costs")
    ax.set_ylabel("thousand USD")
    ax.tick_params(axis="x", rotation=30)
    ax.grid(alpha=0.25)
axes[1].legend(fontsize=7, frameon=False, loc="upper left")
save(fig, "fig2_equity_by_arm")

# Figure 3: paired contrasts with 95% week-block bootstrap intervals.
metrics = [("confidence", "Mean stated confidence"), ("brier", "Brier score of prob_up")]
fig, axes = plt.subplots(1, 2, figsize=(10, 3.2))
fig.subplots_adjust(wspace=0.3)
effects = ["memory", "feeling", "reasoning"]
for ax, (metric, title) in zip(axes, metrics):
    for j, sym in enumerate(SYMBOLS):
        c = con[(con["metric"] == metric) & (con["symbol"] == sym)].set_index("effect").loc[effects]
        y = np.arange(len(effects)) + (0.15 if j == 0 else -0.15)
        ax.errorbar(c["mean_diff"], y, xerr=[c["mean_diff"] - c["ci_low"], c["ci_high"] - c["mean_diff"]],
                    fmt="o" if j == 0 else "s", color="#333" if j == 0 else "#888", capsize=3, label=sym)
        for yy, (_, r) in zip(y, c.iterrows()):
            if r["p_holm"] < 0.05:
                ax.annotate("*", (r["ci_high"], yy), xytext=(4, -4), textcoords="offset points")
    ax.axvline(0, color="k", lw=0.8)
    ax.set_yticks(range(len(effects)))
    ax.set_yticklabels(["memory", "feeling", "reasoning"])
    ax.set_title(f"Paired difference: {title}")
    ax.grid(axis="x", alpha=0.25)
axes[0].legend(frameon=False, fontsize=8)
save(fig, "fig3_contrasts")

# Figure 4: arms against the 15 rule-based strategies.
fig, axes = plt.subplots(1, 2, figsize=(11, 5))
for ax, sym in zip(axes, SYMBOLS):
    s = bl[bl["symbol"] == sym].sort_values("total_return")
    colors = ["#9467bd" if k == "llm" else ("#ff7f0e" if b == "buy_and_hold" else ("#8c564b" if b.startswith("random") else "#bbbbbb")) for k, b in zip(s["kind"], s["baseline"])]
    ax.barh(s["baseline"], s["total_return"], color=colors)
    ax.axvline(0, color="k", lw=0.8)
    ax.set_title(f"{sym}: total return over 60 sessions (%), net of 2 bp")
    ax.tick_params(axis="y", labelsize=7)
    ax.grid(axis="x", alpha=0.25)
save(fig, "fig4_baselines")

# Figure 5: distribution of prob_up per arm against the realized frequency of a rise.
fig, axes = plt.subplots(1, 2, figsize=(9, 3.4), sharey=True)
for ax, sym in zip(axes, SYMBOLS):
    data = [frame[(frame["symbol"] == sym) & (frame["arm"] == arm)]["prob_up"].dropna() for arm in ARMS]
    ax.boxplot(data, tick_labels=ARMS, showmeans=True)
    base = frame[frame["symbol"] == sym].drop_duplicates("slot")["up"].mean()
    ax.axhline(base, color="#ff7f0e", ls="--", lw=1, label=f"realized frequency of a rise ({base:.2f})")
    ax.axhline(0.5, color="k", ls=":", lw=0.8)
    ax.set_ylim(0.3, 0.7)
    ax.tick_params(axis="x", labelsize=7, rotation=15)
    ax.set_title(f"{sym}: stated probability of a rise")
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    ax.grid(axis="y", alpha=0.25)
save(fig, "fig5_prob_up")

# Figure 6: exposure after each decision.
fig, axes = plt.subplots(2, 1, figsize=(10, 4.8), sharex=True)
for ax, sym in zip(axes, SYMBOLS):
    for arm in ARMS:
        a = llm[(llm["symbol"] == sym) & (llm["arm"] == arm)].sort_values("slot")
        ax.step(pd.to_datetime(a["slot"].str[:10]), a["exposure_after"], where="post", color=LLM_COLORS[arm], lw=1, label=arm)
    ax.axhline(0, color="k", lw=0.6)
    ax.set_ylim(-1.05, 1.05); ax.set_ylabel(f"{sym} exposure")
    ax.grid(alpha=0.25)
axes[0].legend(fontsize=7, frameon=False, ncol=4, loc="upper left")
axes[0].set_title("Target exposure held after each decision (-1 fully short, +1 fully long)")
save(fig, "fig6_exposure")

# Figure 7: distribution of stated confidence by arm.
fig, axes = plt.subplots(1, 2, figsize=(9, 3.2), sharey=True)
for ax, sym in zip(axes, SYMBOLS):
    data = [llm[(llm["symbol"] == sym) & (llm["arm"] == arm)]["confidence"].dropna() for arm in ARMS]
    ax.boxplot(data, tick_labels=ARMS, showmeans=True)
    ax.tick_params(axis="x", labelsize=7, rotation=15)
    ax.set_title(f"{sym}: stated confidence per decision")
    ax.grid(axis="y", alpha=0.25)
save(fig, "fig7_confidence")

# Per-arm framework reports (v1 figures 1 to 6 and their companions).
for sym in SYMBOLS:
    for arm in ARMS:
        src = os.path.join(RUN, "report", "arms", f"{sym}_{arm}", "results")
        dst = os.path.join(ARMREP, f"{sym}_{arm}")
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.jsonl", "*_parsed.csv"))
print("\n".join(sorted(os.listdir(FIG))))
print(json.dumps(run_facts, indent=1, default=str))

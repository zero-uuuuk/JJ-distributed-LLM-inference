#!/usr/bin/env python3
"""Visualize KV transfer analysis: hete vs homo comparison charts."""
import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

plt.rcParams.update({"font.size": 11, "figure.dpi": 150})

with open("kv_transfer_analysis_hete_1.json") as f:
    hete = json.load(f)
with open("kv_transfer_analysis_homo_1.json") as f:
    homo = json.load(f)

CASES = {"case1": "Case 1 (in=512, out=128)", "case2": "Case 2 (in=128, out=512)"}

# ── Fig 1: Total KV Transfer Time bar chart (median, mean, p90, p99) ──
fig, axes = plt.subplots(1, 2, figsize=(13, 5))
for idx, (ck, clabel) in enumerate(CASES.items()):
    ax = axes[idx]
    metrics = ["median_ms", "mean_ms", "p90_ms", "p99_ms"]
    labels = ["Median", "Mean", "P90", "P99"]
    homo_vals = [homo[ck]["total_transfer"][m] for m in metrics]
    hete_vals = [hete[ck]["total_transfer"][m] for m in metrics]
    x = np.arange(len(labels))
    w = 0.35
    b1 = ax.bar(x - w/2, homo_vals, w, label="Homo", color="#4C72B0")
    b2 = ax.bar(x + w/2, hete_vals, w, label="Hete", color="#DD8452")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("KV Transfer Time (ms)")
    ax.set_title(clabel)
    ax.legend()
    # value labels
    for bars in [b1, b2]:
        for bar in bars:
            h = bar.get_height()
            txt = f"{h/1000:.1f}s" if h >= 1000 else f"{h:.0f}ms"
            ax.annotate(txt, xy=(bar.get_x() + bar.get_width()/2, h),
                        xytext=(0, 3), textcoords="offset points",
                        ha="center", va="bottom", fontsize=8)
fig.suptitle("KV Transfer Time: Homo vs Hete", fontsize=14, fontweight="bold")
fig.tight_layout()
fig.savefig("plot_kv_total_comparison.png")
print("Saved: plot_kv_total_comparison.png")

# ── Fig 2: Per-layer median transfer time (line chart) ──
fig, axes = plt.subplots(1, 2, figsize=(13, 5))
for idx, (ck, clabel) in enumerate(CASES.items()):
    ax = axes[idx]
    layers = sorted(homo[ck]["per_layer"].keys(), key=int)
    x = [int(l) for l in layers]
    homo_med = [homo[ck]["per_layer"][l]["median_ms"] for l in layers]
    hete_med = [hete[ck]["per_layer"][l]["median_ms"] for l in layers]
    ax.plot(x, homo_med, "o-", label="Homo", color="#4C72B0", markersize=4)
    ax.plot(x, hete_med, "s-", label="Hete", color="#DD8452", markersize=4)
    ax.set_xlabel("Layer")
    ax.set_ylabel("Median Transfer Time (ms)")
    ax.set_title(clabel)
    ax.legend()
    ax.set_xticks(range(0, 32, 4))
fig.suptitle("Per-Layer Median KV Transfer Time", fontsize=14, fontweight="bold")
fig.tight_layout()
fig.savefig("plot_kv_per_layer.png")
print("Saved: plot_kv_per_layer.png")

# ── Fig 3: Per-request CDF ──
fig, axes = plt.subplots(1, 2, figsize=(13, 5))
for idx, (ck, clabel) in enumerate(CASES.items()):
    ax = axes[idx]
    for data, label, color in [(homo, "Homo", "#4C72B0"), (hete, "Hete", "#DD8452")]:
        vals = sorted([r["total_ms"] for r in data[ck]["per_request"]])
        cdf = np.arange(1, len(vals)+1) / len(vals)
        ax.plot(vals, cdf, label=label, color=color, linewidth=1.5)
    ax.set_xlabel("Total KV Transfer Time (ms)")
    ax.set_ylabel("CDF")
    ax.set_title(clabel)
    ax.legend()
    ax.axhline(0.5, color="gray", linestyle="--", alpha=0.4, linewidth=0.8)
    ax.axhline(0.99, color="gray", linestyle="--", alpha=0.4, linewidth=0.8)
    ax.text(ax.get_xlim()[0], 0.5, " p50", fontsize=8, color="gray", va="bottom")
    ax.text(ax.get_xlim()[0], 0.99, " p99", fontsize=8, color="gray", va="bottom")
fig.suptitle("CDF of Per-Request KV Transfer Time", fontsize=14, fontweight="bold")
fig.tight_layout()
fig.savefig("plot_kv_cdf.png")
print("Saved: plot_kv_cdf.png")

# ── Fig 4: E2E impact — KV transfer vs TTFT/TPOT ──
bench_files = {
    "homo_case1": "results_20260402_130725_homo_1/case1_g1.json",
    "homo_case2": "results_20260402_130725_homo_1/case2_g1.json",
    "hete_case1": "results_20260402_134927_hete_1/case1_g1.json",
    "hete_case2": "results_20260402_134927_hete_1/case2_g1.json",
}
bench = {}
for k, p in bench_files.items():
    with open(p) as f:
        bench[k] = json.load(f)

fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))

# TTFT vs KV transfer median
ax = axes[0]
groups = ["Case 1", "Case 2"]
homo_kv = [homo["case1"]["total_transfer"]["median_ms"], homo["case2"]["total_transfer"]["median_ms"]]
hete_kv = [hete["case1"]["total_transfer"]["median_ms"], hete["case2"]["total_transfer"]["median_ms"]]
homo_ttft = [bench["homo_case1"]["median_ttft_ms"], bench["homo_case2"]["median_ttft_ms"]]
hete_ttft = [bench["hete_case1"]["median_ttft_ms"], bench["hete_case2"]["median_ttft_ms"]]

x = np.arange(len(groups))
w = 0.2
ax.bar(x - 1.5*w, homo_kv, w, label="Homo KV Transfer", color="#4C72B0")
ax.bar(x - 0.5*w, homo_ttft, w, label="Homo TTFT", color="#4C72B0", alpha=0.5)
ax.bar(x + 0.5*w, hete_kv, w, label="Hete KV Transfer", color="#DD8452")
ax.bar(x + 1.5*w, hete_ttft, w, label="Hete TTFT", color="#DD8452", alpha=0.5)
ax.set_xticks(x)
ax.set_xticklabels(groups)
ax.set_ylabel("Median Time (ms)")
ax.set_title("KV Transfer vs TTFT (Median)")
ax.legend(fontsize=8)

# TPOT comparison
ax = axes[1]
homo_tpot = [bench["homo_case1"]["median_tpot_ms"], bench["homo_case2"]["median_tpot_ms"]]
hete_tpot = [bench["hete_case1"]["median_tpot_ms"], bench["hete_case2"]["median_tpot_ms"]]
x = np.arange(len(groups))
w = 0.35
ax.bar(x - w/2, homo_tpot, w, label="Homo", color="#4C72B0")
ax.bar(x + w/2, hete_tpot, w, label="Hete", color="#DD8452")
for bars_data, offset in [(homo_tpot, -w/2), (hete_tpot, w/2)]:
    for i, v in enumerate(bars_data):
        ax.text(i + offset, v + 2, f"{v:.1f}", ha="center", fontsize=9)
ax.set_xticks(x)
ax.set_xticklabels(groups)
ax.set_ylabel("Median TPOT (ms)")
ax.set_title("Decode TPOT: Homo vs Hete")
ax.legend()

fig.suptitle("E2E Impact: KV Transfer ↔ Serving Latency", fontsize=14, fontweight="bold")
fig.tight_layout()
fig.savefig("plot_kv_e2e_impact.png")
print("Saved: plot_kv_e2e_impact.png")

# ── Fig 5: Hete/Homo ratio summary (single compact chart) ──
fig, ax = plt.subplots(figsize=(8, 5))
metrics_labels = [
    ("KV Transfer\n(Case1 med)", hete["case1"]["total_transfer"]["median_ms"] / homo["case1"]["total_transfer"]["median_ms"]),
    ("KV Transfer\n(Case1 p99)", hete["case1"]["total_transfer"]["p99_ms"] / homo["case1"]["total_transfer"]["p99_ms"]),
    ("KV Transfer\n(Case2 med)", hete["case2"]["total_transfer"]["median_ms"] / homo["case2"]["total_transfer"]["median_ms"]),
    ("KV Transfer\n(Case2 p99)", hete["case2"]["total_transfer"]["p99_ms"] / homo["case2"]["total_transfer"]["p99_ms"]),
    ("TTFT\n(Case1 med)", bench["hete_case1"]["median_ttft_ms"] / bench["homo_case1"]["median_ttft_ms"]),
    ("TTFT\n(Case2 med)", bench["hete_case2"]["median_ttft_ms"] / bench["homo_case2"]["median_ttft_ms"]),
    ("TPOT\n(Case1 med)", bench["hete_case1"]["median_tpot_ms"] / bench["homo_case1"]["median_tpot_ms"]),
    ("TPOT\n(Case2 med)", bench["hete_case2"]["median_tpot_ms"] / bench["homo_case2"]["median_tpot_ms"]),
]
labels = [m[0] for m in metrics_labels]
ratios = [m[1] for m in metrics_labels]
colors = ["#DD8452" if r > 3 else "#E8A87C" if r > 1.5 else "#82B366" for r in ratios]
bars = ax.barh(range(len(labels)), ratios, color=colors)
ax.set_yticks(range(len(labels)))
ax.set_yticklabels(labels, fontsize=9)
ax.axvline(1.0, color="black", linestyle="-", linewidth=0.8)
ax.set_xlabel("Hete / Homo Ratio")
ax.set_title("Heterogeneous Degradation Ratio", fontsize=13, fontweight="bold")
for i, (bar, r) in enumerate(zip(bars, ratios)):
    ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2,
            f"{r:.1f}x", va="center", fontsize=9, fontweight="bold")
ax.invert_yaxis()
fig.tight_layout()
fig.savefig("plot_kv_hete_ratio.png")
print("Saved: plot_kv_hete_ratio.png")

print("\nDone — 5 charts generated.")

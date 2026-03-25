"""
plot_results.py
────────────────────────────────────────────────────────────
dist_serve_yj Phase-2 실험 결과 시각화
  실험 없이 results/*.json 만 읽어 PNG 그래프를 생성합니다.

생성 파일 (results/ 디렉터리):
  plot_throughput_vs_seqlen.png
  plot_latency_vs_seqlen.png
  plot_gpu_util_vs_seqlen.png
  plot_throughput_vs_batch.png
  plot_heatmap_gpu_util.png
  plot_lm_summary.png   ← 2×2 종합 대시보드
"""

# ── 반드시 pyplot import 전에 선언해야 합니다 ──────────────
import matplotlib
matplotlib.use("Agg")   # headless: GUI 창 없이 파일만 저장

import json
import os
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

# ── 공통 스타일 ────────────────────────────────────────────
matplotlib.rcParams.update({
    "font.family": "DejaVu Sans",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.linestyle": "--",
    "grid.alpha": 0.5,
    "grid.color": "#cccccc",
    "figure.facecolor": "white",
    "axes.facecolor": "#f8f8f8",
    "axes.labelsize": 12,
    "axes.titlesize": 13,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "legend.framealpha": 0.9,
})

# ── 경로 ──────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"
GRID_FILE   = RESULTS_DIR / "phase2_grid.json"

# ── 색상 / 마커 팔레트 ─────────────────────────────────────
BATCH_COLORS  = {1: "#4C72B0", 2: "#DD8452", 4: "#55A868", 8: "#C44E52"}
BATCH_MARKERS = {1: "o", 2: "s", 4: "^", 8: "D"}
SEQ_COLORS    = {
    128:  "#4C72B0",
    256:  "#DD8452",
    512:  "#55A868",
    1024: "#C44E52",
    1536: "#8172B2",
    2048: "#937860",
}


# ══════════════════════════════════════════════════════════
# 데이터 헬퍼
# ══════════════════════════════════════════════════════════
def load_grid():
    with open(GRID_FILE, encoding="utf-8") as f:
        return [r for r in json.load(f) if not r.get("oom")]


def by_batch(data):
    """batch_size → sorted rows"""
    g = {}
    for r in data:
        g.setdefault(r["batch_size"], []).append(r)
    return {k: sorted(v, key=lambda x: x["seq_len"]) for k, v in sorted(g.items())}


def by_seq(data):
    """seq_len → sorted rows"""
    g = {}
    for r in data:
        g.setdefault(r["seq_len"], []).append(r)
    return {k: sorted(v, key=lambda x: x["batch_size"]) for k, v in sorted(g.items())}


# ══════════════════════════════════════════════════════════
# 그래프 1: Throughput vs Seq Len
# ══════════════════════════════════════════════════════════
def plot_throughput_vs_seqlen(data):
    fig, ax = plt.subplots(figsize=(9, 5))

    for b, rows in by_batch(data).items():
        xs = [r["seq_len"] for r in rows]
        ys = [r["throughput_tokens_per_sec"] / 1000 for r in rows]
        ax.plot(xs, ys,
                color=BATCH_COLORS[b], marker=BATCH_MARKERS[b],
                linewidth=2, markersize=7, label=f"B={b}")

    # 추정 Lm 수직선
    for b, lm in {1: 1536, 2: 1024, 4: 512, 8: 512}.items():
        ax.axvline(lm, color=BATCH_COLORS[b], linestyle=":", linewidth=1.2, alpha=0.6)

    ax.set_xlabel("Prompt Length (tokens)")
    ax.set_ylabel("Throughput (k tokens / sec)")
    ax.set_title("Prefill Throughput vs Prompt Length  |  OPT-6.7B · T4 GPU")
    ax.set_xticks([128, 256, 512, 1024, 1536, 2048])
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
    ax.legend(title="Batch Size", loc="lower right")
    fig.tight_layout()
    _save(fig, "plot_throughput_vs_seqlen.png")


# ══════════════════════════════════════════════════════════
# 그래프 2: Latency vs Seq Len  (±1σ 밴드 포함)
# ══════════════════════════════════════════════════════════
def plot_latency_vs_seqlen(data):
    fig, ax = plt.subplots(figsize=(9, 5))

    for b, rows in by_batch(data).items():
        xs  = [r["seq_len"] for r in rows]
        ys  = [r["mean_latency_sec"] * 1000 for r in rows]
        std = [r["std_latency_sec"]  * 1000 for r in rows]
        lo  = [y - s for y, s in zip(ys, std)]
        hi  = [y + s for y, s in zip(ys, std)]
        c   = BATCH_COLORS[b]
        ax.plot(xs, ys, color=c, marker=BATCH_MARKERS[b],
                linewidth=2, markersize=7, label=f"B={b}")
        ax.fill_between(xs, lo, hi, color=c, alpha=0.15)

    ax.set_xlabel("Prompt Length (tokens)")
    ax.set_ylabel("Prefill Latency (ms)")
    ax.set_title("Prefill Latency vs Prompt Length  |  shaded = ±1σ")
    ax.set_xticks([128, 256, 512, 1024, 1536, 2048])
    ax.legend(title="Batch Size", loc="upper left")
    fig.tight_layout()
    _save(fig, "plot_latency_vs_seqlen.png")


# ══════════════════════════════════════════════════════════
# 그래프 3: GPU Utilization vs Seq Len
# ══════════════════════════════════════════════════════════
def plot_gpu_util_vs_seqlen(data):
    fig, ax = plt.subplots(figsize=(9, 5))

    for b, rows in by_batch(data).items():
        xs  = [r["seq_len"] for r in rows]
        ys  = [r["gpu_util_mean_pct"] for r in rows]
        ymax= [r["gpu_util_max_pct"]  for r in rows]
        c   = BATCH_COLORS[b]
        ax.plot(xs, ys, color=c, marker=BATCH_MARKERS[b],
                linewidth=2, markersize=7, label=f"B={b}")
        ax.fill_between(xs, ys, ymax, color=c, alpha=0.12)

    ax.axhline(80, color="gray", linestyle="--", linewidth=1.1, label="80% ref")
    ax.set_xlabel("Prompt Length (tokens)")
    ax.set_ylabel("GPU Utilization (%)")
    ax.set_title("GPU Utilization vs Prompt Length  |  shaded = mean → max")
    ax.set_xticks([128, 256, 512, 1024, 1536, 2048])
    ax.set_ylim(0, 105)
    ax.legend(title="Batch Size", loc="lower right")
    fig.tight_layout()
    _save(fig, "plot_gpu_util_vs_seqlen.png")


# ══════════════════════════════════════════════════════════
# 그래프 4: Throughput vs Batch Size
# ══════════════════════════════════════════════════════════
def plot_throughput_vs_batch(data):
    fig, ax = plt.subplots(figsize=(9, 5))

    for s, rows in by_seq(data).items():
        xs = [r["batch_size"] for r in rows]
        ys = [r["throughput_tokens_per_sec"] / 1000 for r in rows]
        ax.plot(xs, ys,
                color=SEQ_COLORS.get(s, "#333333"),
                marker="o", linewidth=2, markersize=7, label=f"L={s}")

    ax.set_xlabel("Batch Size")
    ax.set_ylabel("Throughput (k tokens / sec)")
    ax.set_title("Prefill Throughput vs Batch Size  |  OPT-6.7B · T4 GPU")
    ax.set_xticks([1, 2, 4, 8])
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
    ax.legend(title="Seq Len", loc="lower right")
    fig.tight_layout()
    _save(fig, "plot_throughput_vs_batch.png")


# ══════════════════════════════════════════════════════════
# 그래프 5: GPU Utilization 히트맵
# ══════════════════════════════════════════════════════════
def plot_heatmap(data):
    batches = sorted({r["batch_size"] for r in data})
    seqs    = sorted({r["seq_len"]    for r in data})
    lookup  = {(r["batch_size"], r["seq_len"]): r for r in data}

    mat = np.full((len(batches), len(seqs)), np.nan)
    for i, b in enumerate(batches):
        for j, s in enumerate(seqs):
            row = lookup.get((b, s))
            if row:
                mat[i, j] = row["gpu_util_mean_pct"]

    fig, ax = plt.subplots(figsize=(10, 4))
    im = ax.imshow(mat, aspect="auto", cmap="YlOrRd", vmin=0, vmax=100)
    fig.colorbar(im, ax=ax, label="GPU Utilization (%)")

    ax.set_xticks(range(len(seqs)));   ax.set_xticklabels(seqs)
    ax.set_yticks(range(len(batches)));ax.set_yticklabels([f"B={b}" for b in batches])
    ax.set_xlabel("Prompt Length (tokens)")
    ax.set_title("GPU Utilization Heatmap (B × L)  |  OPT-6.7B · T4 GPU")

    for i in range(len(batches)):
        for j in range(len(seqs)):
            v = mat[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v:.0f}%", ha="center", va="center",
                        fontsize=9, fontweight="bold",
                        color="white" if v > 65 else "black")
    fig.tight_layout()
    _save(fig, "plot_heatmap_gpu_util.png")


# ══════════════════════════════════════════════════════════
# 그래프 6: 2×2 종합 대시보드 (레퍼런스 이미지 스타일)
# ══════════════════════════════════════════════════════════
def plot_dashboard(data):
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    fig.suptitle(
        "Prefill Compute-Bound Analysis  |  OPT-6.7B · T4 GPU (g4dn.xlarge)",
        fontsize=14, fontweight="bold")

    gb = by_batch(data)
    gs = by_seq(data)

    # ① Throughput vs SeqLen
    ax = axes[0, 0]
    for b, rows in gb.items():
        xs = [r["seq_len"] for r in rows]
        ys = [r["throughput_tokens_per_sec"] / 1000 for r in rows]
        ax.plot(xs, ys, color=BATCH_COLORS[b], marker=BATCH_MARKERS[b],
                lw=2, ms=6, label=f"B={b}")
    for b, lm in {1: 1536, 2: 1024, 4: 512, 8: 512}.items():
        ax.axvline(lm, color=BATCH_COLORS[b], ls=":", lw=1.1, alpha=0.6)
    ax.set(xlabel="Prompt Len (tokens)", ylabel="Throughput (k tok/s)",
           title="① Throughput vs Seq Len")
    ax.set_xticks([128, 256, 512, 1024, 1536, 2048])
    ax.tick_params(axis="x", rotation=25)
    ax.legend(title="Batch", fontsize=8, loc="lower right")

    # ② Latency vs SeqLen  ±1σ 밴드
    ax = axes[0, 1]
    for b, rows in gb.items():
        xs  = [r["seq_len"] for r in rows]
        ys  = [r["mean_latency_sec"] * 1000 for r in rows]
        std = [r["std_latency_sec"]  * 1000 for r in rows]
        c = BATCH_COLORS[b]
        ax.plot(xs, ys, color=c, marker=BATCH_MARKERS[b], lw=2, ms=6, label=f"B={b}")
        ax.fill_between(xs,
                        [y - s for y, s in zip(ys, std)],
                        [y + s for y, s in zip(ys, std)],
                        color=c, alpha=0.15)
    ax.set(xlabel="Prompt Len (tokens)", ylabel="Latency (ms)",
           title="② Latency vs Seq Len  (±1σ)")
    ax.set_xticks([128, 256, 512, 1024, 1536, 2048])
    ax.tick_params(axis="x", rotation=25)
    ax.legend(title="Batch", fontsize=8, loc="upper left")

    # ③ GPU Util vs SeqLen
    ax = axes[1, 0]
    for b, rows in gb.items():
        xs   = [r["seq_len"] for r in rows]
        ys   = [r["gpu_util_mean_pct"] for r in rows]
        ymax = [r["gpu_util_max_pct"]  for r in rows]
        c = BATCH_COLORS[b]
        ax.plot(xs, ys, color=c, marker=BATCH_MARKERS[b], lw=2, ms=6, label=f"B={b}")
        ax.fill_between(xs, ys, ymax, color=c, alpha=0.12)
    ax.axhline(80, color="gray", ls="--", lw=1.0, alpha=0.8, label="80% ref")
    ax.set(xlabel="Prompt Len (tokens)", ylabel="GPU Util (%)",
           title="③ GPU Utilization vs Seq Len", ylim=(0, 105))
    ax.set_xticks([128, 256, 512, 1024, 1536, 2048])
    ax.tick_params(axis="x", rotation=25)
    ax.legend(title="Batch", fontsize=8, loc="lower right")

    # ④ Throughput vs Batch Size
    ax = axes[1, 1]
    for s, rows in gs.items():
        xs = [r["batch_size"] for r in rows]
        ys = [r["throughput_tokens_per_sec"] / 1000 for r in rows]
        ax.plot(xs, ys, color=SEQ_COLORS.get(s, "#333"),
                marker="o", lw=2, ms=6, label=f"L={s}")
    ax.set(xlabel="Batch Size", ylabel="Throughput (k tok/s)",
           title="④ Throughput vs Batch Size")
    ax.set_xticks([1, 2, 4, 8])
    ax.legend(title="Seq Len", fontsize=8, loc="lower right")

    fig.tight_layout()
    _save(fig, "plot_lm_summary.png")


# ══════════════════════════════════════════════════════════
# 저장 헬퍼
# ══════════════════════════════════════════════════════════
def _save(fig, name):
    out = RESULTS_DIR / name
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved → {out}")


# ══════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("Loading phase2_grid.json ...")
    data = load_grid()
    print(f"  {len(data)} records loaded.\n")

    print("Generating plots ...")
    plot_throughput_vs_seqlen(data)
    plot_latency_vs_seqlen(data)
    plot_gpu_util_vs_seqlen(data)
    plot_throughput_vs_batch(data)
    plot_heatmap(data)
    plot_dashboard(data)

    print(f"\nAll done! → {RESULTS_DIR}")

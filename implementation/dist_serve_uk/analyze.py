"""
analyze.py
----------
실험 결과 JSON을 읽어 Prefill-Decoding 간섭 효과를 분석하고 시각화한다.

출력:
  - 콘솔: iter 0 TPOT 비교 및 순수 prefill 간섭 효과
  - 그래프: results/plot_batch{N}.png (iter별 TPOT 시계열)
"""

import json
import pathlib
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

RESULT_DIR = pathlib.Path(__file__).parent / "result"
PLOT_DIR = pathlib.Path(__file__).parent / "result"

# ms 단위로 변환
MS = 1000


def load(batch_size: int) -> dict:
    path = RESULT_DIR / f"tpot_batch{batch_size}.json"
    with open(path) as f:
        return json.load(f)


def analyze(batch_size: int, data: dict, lines: list = None):
    def log(s=""):
        print(s)
        if lines is not None:
            lines.append(s)

    log(f"\n{'='*50}")
    log(f"batch_size = {batch_size}")
    log(f"{'='*50}")

    b_mean  = np.array(data["baseline"]["mean"]) * MS
    b_std   = np.array(data["baseline"]["std"]) * MS
    t_mean  = np.array(data["treatment"]["mean"]) * MS
    t_std   = np.array(data["treatment"]["std"]) * MS

    log(f"\n[iter 0 TPOT]")
    log(f"  Baseline   : {b_mean[0]:.2f} ± {b_std[0]:.2f} ms")
    log(f"  Treatment  : {t_mean[0]:.2f} ± {t_std[0]:.2f} ms")
    log(f"  Treatment - Baseline (배치크기+prefill 효과): {t_mean[0] - b_mean[0]:+.2f} ms")

    if "baseline_plus1" in data:
        bp1_mean = np.array(data["baseline_plus1"]["mean"]) * MS
        bp1_std  = np.array(data["baseline_plus1"]["std"]) * MS
        log(f"  Baseline+1 : {bp1_mean[0]:.2f} ± {bp1_std[0]:.2f} ms")
        log(f"  Treatment - Baseline+1 (순수 prefill 간섭): {t_mean[0] - bp1_mean[0]:+.2f} ms")

    stable_b  = b_mean[10:50].mean()
    stable_t  = t_mean[10:50].mean()
    log(f"\n[안정 구간 iter 10~50 평균 TPOT]")
    log(f"  Baseline  : {stable_b:.2f} ms")
    log(f"  Treatment : {stable_t:.2f} ms")
    log(f"  차이       : {stable_t - stable_b:+.2f} ms")


def plot(batch_size: int, data: dict):
    fig, ax = plt.subplots(figsize=(12, 5))

    b_mean = np.array(data["baseline"]["mean"]) * MS
    b_std  = np.array(data["baseline"]["std"]) * MS
    t_mean = np.array(data["treatment"]["mean"]) * MS
    t_std  = np.array(data["treatment"]["std"]) * MS
    iters  = np.arange(len(b_mean))

    ax.plot(iters, b_mean, label="Baseline", color="steelblue")
    ax.fill_between(iters, b_mean - b_std, b_mean + b_std, alpha=0.2, color="steelblue")

    ax.plot(iters, t_mean, label="Treatment", color="tomato")
    ax.fill_between(iters, t_mean - t_std, t_mean + t_std, alpha=0.2, color="tomato")

    if "baseline_plus1" in data:
        bp1_mean = np.array(data["baseline_plus1"]["mean"]) * MS
        bp1_std  = np.array(data["baseline_plus1"]["std"]) * MS
        ax.plot(iters, bp1_mean, label="Baseline+1", color="seagreen", linestyle="--")
        ax.fill_between(iters, bp1_mean - bp1_std, bp1_mean + bp1_std, alpha=0.15, color="seagreen")

    ax.axvline(x=0, color="gray", linestyle=":", linewidth=1, label="iter 0 (prefill 발생)")
    ax.set_xlabel("Iteration")
    ax.set_ylabel("TPOT (ms)")
    ax.set_title(f"Prefill-Decoding Interference  |  batch_size={batch_size}")
    ax.legend()
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.1f"))
    ax.grid(True, alpha=0.3)

    # iter 0 spike가 크면 나머지 구간이 안 보이므로 두 번째 axes로 zoom-in
    fig2, ax2 = plt.subplots(figsize=(12, 4))
    zoom_end = min(20, len(b_mean))
    ax2.plot(iters[:zoom_end], b_mean[:zoom_end], label="Baseline", color="steelblue")
    ax2.fill_between(iters[:zoom_end], (b_mean - b_std)[:zoom_end], (b_mean + b_std)[:zoom_end], alpha=0.2, color="steelblue")
    ax2.plot(iters[:zoom_end], t_mean[:zoom_end], label="Treatment", color="tomato")
    ax2.fill_between(iters[:zoom_end], (t_mean - t_std)[:zoom_end], (t_mean + t_std)[:zoom_end], alpha=0.2, color="tomato")
    if "baseline_plus1" in data:
        ax2.plot(iters[:zoom_end], bp1_mean[:zoom_end], label="Baseline+1", color="seagreen", linestyle="--")
        ax2.fill_between(iters[:zoom_end], (bp1_mean - bp1_std)[:zoom_end], (bp1_mean + bp1_std)[:zoom_end], alpha=0.15, color="seagreen")
    ax2.set_xlabel("Iteration")
    ax2.set_ylabel("TPOT (ms)")
    ax2.set_title(f"Zoom: iter 0~{zoom_end-1}  |  batch_size={batch_size}")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    out1 = PLOT_DIR / f"plot_batch{batch_size}_full.png"
    out2 = PLOT_DIR / f"plot_batch{batch_size}_zoom.png"
    fig.savefig(out1, dpi=150, bbox_inches="tight")
    fig2.savefig(out2, dpi=150, bbox_inches="tight")
    plt.close("all")
    print(f"  → {out1.name}, {out2.name} 저장")


def plot_interference_summary(all_data: dict):
    """
    DistServe Figure 스타일: X축=Batch Size, Y축=iter 0 TPOT.
    decoding slowdown / prefill slowdown 화살표 표시.
    """
    batch_sizes = sorted(all_data.keys())

    baseline_iter0   = [np.array(all_data[b]["baseline"]["mean"])[0]   * MS for b in batch_sizes]
    treatment_iter0  = [np.array(all_data[b]["treatment"]["mean"])[0]  * MS for b in batch_sizes]
    baseline_err     = [np.array(all_data[b]["baseline"]["std"])[0]    * MS for b in batch_sizes]
    treatment_err    = [np.array(all_data[b]["treatment"]["std"])[0]   * MS for b in batch_sizes]

    fig, ax = plt.subplots(figsize=(7, 5))

    ax.errorbar(batch_sizes, treatment_iter0, yerr=treatment_err,
                marker="o", color="steelblue", label="decoding-with-one-prefill", capsize=4)
    ax.errorbar(batch_sizes, baseline_iter0, yerr=baseline_err,
                marker="o", color="darkorange", label="decoding-only", capsize=4)

    # 점선: batch_sizes[0] 기준 treatment 값
    ref_val = treatment_iter0[0]
    ax.axhline(y=ref_val, color="steelblue", linestyle="--", linewidth=1)

    # 화살표: 가장 작은 batch에서 decoding slowdown, 가장 큰 batch에서 prefill slowdown
    small_b_idx = 0
    large_b_idx = len(batch_sizes) - 1

    # decoding slowdown: baseline → treatment (작은 batch)
    ax.annotate("", xy=(batch_sizes[small_b_idx], treatment_iter0[small_b_idx]),
                xytext=(batch_sizes[small_b_idx], baseline_iter0[small_b_idx]),
                arrowprops=dict(arrowstyle="<->", color="black", lw=1.2))
    ax.text(batch_sizes[small_b_idx] + 0.5, (treatment_iter0[small_b_idx] + baseline_iter0[small_b_idx]) / 2,
            "decoding\nslowdown", fontsize=8, va="center")

    # prefill slowdown: ref_val → treatment (큰 batch)
    ax.annotate("", xy=(batch_sizes[large_b_idx], treatment_iter0[large_b_idx]),
                xytext=(batch_sizes[large_b_idx], ref_val),
                arrowprops=dict(arrowstyle="<->", color="black", lw=1.2))
    ax.text(batch_sizes[large_b_idx] + 0.5, (treatment_iter0[large_b_idx] + ref_val) / 2,
            "prefill\nslowdown", fontsize=8, va="center")

    ax.set_xlabel("Batch Size")
    ax.set_ylabel("Latency (ms)  [iter 0 TPOT]")
    ax.set_title(f"Prefill-Decoding Interference  (Prefill input={512} tokens)")
    ax.set_xticks(batch_sizes)
    ax.legend()
    ax.grid(True, alpha=0.3)

    out = PLOT_DIR / "plot_interference_summary.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  → {out.name} 저장")


if __name__ == "__main__":
    lines = []
    all_data = {}

    for batch_size in [8, 16, 32]:
        path = RESULT_DIR / f"tpot_batch{batch_size}.json"
        if not path.exists():
            print(f"[skip] tpot_batch{batch_size}.json 없음")
            continue
        data = load(batch_size)
        all_data[batch_size] = data
        analyze(batch_size, data, lines)
        plot(batch_size, data)

    if all_data:
        plot_interference_summary(all_data)

    out_path = PLOT_DIR / "analysis_result.txt"
    out_path.write_text("\n".join(lines))
    print(f"\n결과 저장: {out_path}")

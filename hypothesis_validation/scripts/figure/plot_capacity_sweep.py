"""Plot GPU KV cache capacity sweep metrics."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_RUNS = [
    ("0.96", "mixed_chat5_rag5_longctx1500_util096.jsonl", "eviction_mixed_chat5_rag5_longctx1500_util096.jsonl"),
    ("0.7", "mixed_chat5_rag5_longctx1500_util07.jsonl", "eviction_mixed_chat5_rag5_longctx1500_util07.jsonl"),
    ("0.6", "mixed_chat5_rag5_longctx1500_util06.jsonl", "eviction_mixed_chat5_rag5_longctx1500_util06.jsonl"),
    ("0.5", "mixed_chat5_rag5_longctx1500_util05.jsonl", "eviction_mixed_chat5_rag5_longctx1500_util05.jsonl"),
]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as input_file:
        return [json.loads(line) for line in input_file if line.strip()]


def percentile(values: list[float], q: float) -> float | None:
    clean = [value for value in values if value is not None and not math.isnan(value)]
    if not clean:
        return None
    clean.sort()
    index = (len(clean) - 1) * q / 100.0
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return clean[low]
    return clean[low] * (high - index) + clean[high] * (index - low)


def prompt_tokens(row: dict[str, Any]) -> int:
    return int(row.get("prompt_tokens") or row.get("l_r") or 0)


def cached_tokens(row: dict[str, Any]) -> int:
    value = row.get("cached_tokens")
    if value is None:
        value = row.get("h_r")
    return int(value or 0)


def summarize_workload(rows: list[dict[str, Any]], workload: str, slo_s: float) -> dict[str, float]:
    selected = [row for row in rows if row.get("workload") == workload and not row.get("error")]
    total_prompt = sum(prompt_tokens(row) for row in selected)
    total_cached = sum(cached_tokens(row) for row in selected)
    ttfts = [float(row["ttft"]) for row in selected if row.get("ttft") is not None]
    return {
        "token_hit_rate": (total_cached / total_prompt) if total_prompt else 0.0,
        "ttft_p50_s": percentile(ttfts, 50) or 0.0,
        "ttft_p95_s": percentile(ttfts, 95) or 0.0,
        "ttft_p99_s": percentile(ttfts, 99) or 0.0,
        "slo_attainment": (sum(1 for value in ttfts if value <= slo_s) / len(selected)) if selected else 0.0,
    }


def summarize_evictions(rows: list[dict[str, Any]]) -> dict[str, float]:
    pairs: Counter[tuple[str, str]] = Counter()
    useful: Counter[tuple[str, str]] = Counter()
    for row in rows:
        pair = (str(row.get("evicted_workload")), str(row.get("trigger_workload")))
        pairs[pair] += 1
        if row.get("reused_later"):
            useful[pair] += 1

    total = sum(pairs.values())
    chat_rag_total = pairs[("chat", "rag")]
    chat_rag_useful = useful[("chat", "rag")]
    chat_chat_useful = useful[("chat", "chat")]
    useful_chat_total = chat_chat_useful + chat_rag_useful
    return {
        "total_evictions": float(total),
        "chat_rag_evictions": float(chat_rag_total),
        "chat_rag_useful": float(chat_rag_useful),
        "chat_rag_useful_rate": (chat_rag_useful / chat_rag_total) if chat_rag_total else 0.0,
        "rag_share_of_useful_chat_evictions": (chat_rag_useful / useful_chat_total) if useful_chat_total else 0.0,
    }


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def plot(summary: list[dict[str, Any]], output_dir: Path) -> None:
    import matplotlib.pyplot as plt

    labels = [row["util"] for row in summary]
    x = list(range(len(labels)))
    chat_hit = [row["chat_token_hit_rate"] * 100.0 for row in summary]
    chat_slo = [row["chat_slo_attainment"] * 100.0 for row in summary]
    rag_slo = [row["rag_slo_attainment"] * 100.0 for row in summary]
    useful = [row["chat_rag_useful"] for row in summary]

    output_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))

    axes[0].plot(x, chat_hit, marker="o")
    axes[0].set_xticks(x, labels)
    axes[0].set_ylabel("Chat token hit rate (%)")
    axes[0].set_title("Chat cache locality")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(x, chat_slo, marker="o", label="Chat SLO")
    axes[1].plot(x, rag_slo, marker="s", label="RAG SLO")
    axes[1].set_xticks(x, labels)
    axes[1].set_ylim(0, 105)
    axes[1].set_ylabel("SLO attainment (%)")
    axes[1].set_title("SLO under lower KV capacity")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    axes[2].bar(labels, useful)
    axes[2].set_ylabel("Useful chat<-RAG evictions")
    axes[2].set_title("Useful cross-workload eviction")
    axes[2].grid(True, axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(output_dir / "capacity_sweep.png", dpi=200)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot GPU memory utilization sweep.")
    parser.add_argument("--results-dir", type=Path, default=Path("../../results"))
    parser.add_argument("--output-dir", type=Path, default=Path("../../results/figures"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results_dir = args.results_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    summary: list[dict[str, Any]] = []
    for util, result_name, eviction_name in DEFAULT_RUNS:
        result_rows = load_jsonl(results_dir / result_name)
        eviction_rows = load_jsonl(results_dir / eviction_name)
        if not result_rows:
            print(f"skip missing result: {results_dir / result_name}")
            continue
        chat = summarize_workload(result_rows, "chat", slo_s=0.5)
        rag = summarize_workload(result_rows, "rag", slo_s=2.0)
        eviction = summarize_evictions(eviction_rows)
        summary.append(
            {
                "util": util,
                "chat_token_hit_rate": chat["token_hit_rate"],
                "chat_ttft_p95_s": chat["ttft_p95_s"],
                "chat_slo_attainment": chat["slo_attainment"],
                "rag_token_hit_rate": rag["token_hit_rate"],
                "rag_ttft_p95_s": rag["ttft_p95_s"],
                "rag_slo_attainment": rag["slo_attainment"],
                **eviction,
            }
        )

    write_summary_csv(output_dir / "capacity_sweep_summary.csv", summary)
    if summary:
        try:
            plot(summary, output_dir)
        except ModuleNotFoundError as exc:
            if exc.name != "matplotlib":
                raise
            print("skip PNG: install matplotlib to generate figures")
    print(f"wrote outputs to {output_dir}")


if __name__ == "__main__":
    main()

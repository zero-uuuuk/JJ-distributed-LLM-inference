"""Plot turn-level Chat SLO collapse for victim workload experiments."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_RUNS = [
    ("ctx500", "mixed_chat5_rag5_longctx500_util06.jsonl"),
    ("ctx1500", "mixed_chat5_rag5_longctx1500_util06.jsonl"),
    ("ctx3000", "mixed_chat5_rag5_longctx3000_util06.jsonl"),
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


def summarize_turns(rows: list[dict[str, Any]], label: str, slo_s: float) -> list[dict[str, Any]]:
    by_turn: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("workload") != "chat" or row.get("error"):
            continue
        turn_id = row.get("turn_id")
        if turn_id is not None:
            by_turn[int(turn_id)].append(row)

    summary: list[dict[str, Any]] = []
    for turn_id in sorted(by_turn):
        turn_rows = by_turn[turn_id]
        total_prompt = sum(prompt_tokens(row) for row in turn_rows)
        total_cached = sum(cached_tokens(row) for row in turn_rows)
        ttfts = [float(row["ttft"]) for row in turn_rows if row.get("ttft") is not None]
        summary.append(
            {
                "label": label,
                "turn_id": turn_id,
                "count": len(turn_rows),
                "token_hit_rate": (total_cached / total_prompt) if total_prompt else 0.0,
                "ttft_p50_s": percentile(ttfts, 50) or 0.0,
                "ttft_p95_s": percentile(ttfts, 95) or 0.0,
                "slo_attainment": (sum(1 for value in ttfts if value <= slo_s) / len(turn_rows)) if turn_rows else 0.0,
            }
        )
    return summary


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

    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summary:
        by_label[str(row["label"])].append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))

    for label, rows in by_label.items():
        rows.sort(key=lambda row: int(row["turn_id"]))
        turns = [row["turn_id"] for row in rows]
        slo = [row["slo_attainment"] * 100.0 for row in rows]
        p95 = [row["ttft_p95_s"] for row in rows]
        axes[0].plot(turns, slo, marker="o", label=label)
        axes[1].plot(turns, p95, marker="o", label=label)

    axes[0].set_xlabel("Chat turn")
    axes[0].set_ylabel("SLO attainment (%)")
    axes[0].set_ylim(0, 105)
    axes[0].set_title("Turn-level Chat SLO")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend()

    axes[1].set_xlabel("Chat turn")
    axes[1].set_ylabel("TTFT P95 (s)")
    axes[1].set_title("Turn-level Chat TTFT tail")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend()

    fig.tight_layout()
    fig.savefig(output_dir / "turn_level_slo.png", dpi=200)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot turn-level Chat SLO curves.")
    parser.add_argument("--results-dir", type=Path, default=Path("../../results"))
    parser.add_argument("--output-dir", type=Path, default=Path("../../results/figures"))
    parser.add_argument(
        "--isolated",
        default="chat_isolated_turn9_qps5.jsonl",
        help="Optional Chat-only baseline filename under results-dir.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results_dir = args.results_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    summary: list[dict[str, Any]] = []

    isolated_rows = load_jsonl(results_dir / args.isolated)
    if isolated_rows:
        for row in isolated_rows:
            row.setdefault("workload", "chat")
        summary.extend(summarize_turns(isolated_rows, "isolated", slo_s=0.5))
    else:
        print(f"skip missing isolated baseline: {results_dir / args.isolated}")

    for label, filename in DEFAULT_RUNS:
        rows = load_jsonl(results_dir / filename)
        if not rows:
            print(f"skip missing result: {results_dir / filename}")
            continue
        summary.extend(summarize_turns(rows, label, slo_s=0.5))

    write_summary_csv(output_dir / "turn_level_slo_summary.csv", summary)
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

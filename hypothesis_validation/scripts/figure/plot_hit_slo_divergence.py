"""Plot hit-ratio vs SLO-goodput divergence for mixed workload cache pressure.

This figure highlights the key motivation for QuotaCache: RAG prefix hit ratio
can stay high or increase while Chat SLO goodput falls, because hit prefix KV
blocks still occupy GPU memory and evict future-reusable Chat prefixes.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any


def load_summary(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"missing summary CSV: {path}")
    with path.open("r", encoding="utf-8", newline="") as input_file:
        return list(csv.DictReader(input_file))


def as_float(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if value is None or value == "":
        return 0.0
    return float(value)


def plot(rows: list[dict[str, Any]], output_dir: Path, output_name: str) -> None:
    import matplotlib.pyplot as plt

    labels = [str(row["label"]) for row in rows]
    x = list(range(len(labels)))

    rag_hit = [as_float(row, "rag_token_hit_rate") * 100.0 for row in rows]
    chat_slo = [as_float(row, "chat_slo_attainment") * 100.0 for row in rows]
    useful_evictions = [as_float(row, "chat_rag_useful") for row in rows]
    rag_share = [as_float(row, "rag_share_of_useful_chat_evictions") * 100.0 for row in rows]

    output_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))

    left = axes[0]
    right = left.twinx()

    hit_line = left.plot(x, rag_hit, marker="s", color="tab:orange", label="RAG prefix hit ratio")
    slo_line = right.plot(x, chat_slo, marker="o", color="tab:blue", label="Chat SLO goodput")

    left.set_xticks(x, labels)
    left.set_ylim(0, 105)
    right.set_ylim(0, 105)
    left.set_ylabel("RAG prefix hit ratio (%)", color="tab:orange")
    right.set_ylabel("Chat SLO goodput (%)", color="tab:blue")
    left.tick_params(axis="y", labelcolor="tab:orange")
    right.tick_params(axis="y", labelcolor="tab:blue")
    left.set_title("Hit ratio and SLO goodput diverge")
    left.grid(True, axis="y", alpha=0.3)

    lines = hit_line + slo_line
    left.legend(lines, [line.get_label() for line in lines], loc="lower left")

    bars = axes[1].bar(labels, useful_evictions, color="tab:red", alpha=0.85)
    axes[1].set_ylabel("Useful Chat blocks evicted by RAG")
    axes[1].set_title("RAG evicts future-reusable Chat KV")
    axes[1].grid(True, axis="y", alpha=0.3)

    for bar, share in zip(bars, rag_share, strict=True):
        axes[1].text(
            bar.get_x() + bar.get_width() / 2.0,
            bar.get_height(),
            f"{share:.1f}%",
            ha="center",
            va="bottom",
            fontsize=9,
        )

    fig.tight_layout()
    fig.savefig(output_dir / output_name, dpi=200)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot prefix hit-ratio vs Chat SLO-goodput divergence.")
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("../../results/figures/context_length_sweep_summary.csv"),
        help="Context-length sweep summary CSV.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("../../results/figures"))
    parser.add_argument("--output-name", default="hit_slo_divergence.png")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary_path = args.summary.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    rows = load_summary(summary_path)
    plot(rows, output_dir, args.output_name)
    print(f"wrote {output_dir / args.output_name}")


if __name__ == "__main__":
    main()

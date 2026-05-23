"""Print useful KV-cache eviction attribution from eviction JSONL files."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, TextIO


DEFAULT_RUNS = [
    ("ctx500", "eviction_mixed_chat5_rag5_longctx500_util06.jsonl"),
    ("ctx1500", "eviction_mixed_chat5_rag5_longctx1500_util06.jsonl"),
    ("ctx3000", "eviction_mixed_chat5_rag5_longctx3000_util06.jsonl"),
]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as input_file:
        for line in input_file:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def pct(numerator: float, denominator: float) -> float:
    return (numerator / denominator * 100.0) if denominator else 0.0


def summarize_evictions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_by_pair: Counter[tuple[str, str]] = Counter()
    useful_by_pair: Counter[tuple[str, str]] = Counter()

    for row in rows:
        pair = (str(row.get("evicted_workload")), str(row.get("trigger_workload")))
        total_by_pair[pair] += 1
        if row.get("reused_later"):
            useful_by_pair[pair] += 1

    total_evictions = sum(total_by_pair.values())
    total_useful = sum(useful_by_pair.values())
    cross_evictions = sum(count for (evicted, trigger), count in total_by_pair.items() if evicted != trigger)
    useful_cross_evictions = sum(count for (evicted, trigger), count in useful_by_pair.items() if evicted != trigger)

    chat_chat_useful = useful_by_pair[("chat", "chat")]
    chat_rag_total = total_by_pair[("chat", "rag")]
    chat_rag_useful = useful_by_pair[("chat", "rag")]
    useful_chat_evictions = chat_chat_useful + chat_rag_useful

    return {
        "total_by_pair": total_by_pair,
        "useful_by_pair": useful_by_pair,
        "total_evictions": total_evictions,
        "total_useful": total_useful,
        "cross_evictions": cross_evictions,
        "useful_cross_evictions": useful_cross_evictions,
        "chat_rag_total": chat_rag_total,
        "chat_rag_useful": chat_rag_useful,
        "chat_rag_useful_rate": pct(chat_rag_useful, chat_rag_total),
        "rag_share_of_useful_chat_evictions": pct(chat_rag_useful, useful_chat_evictions),
    }


def format_key_metrics(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "",
            "Key useful-cache metrics",
            "-" * 56,
            f"total evictions:                     {summary['total_evictions']:,}",
            (
                f"total useful evictions:              {summary['total_useful']:,} "
                f"({pct(summary['total_useful'], summary['total_evictions']):.2f}%)"
            ),
            (
                f"cross-workload evictions:            {summary['cross_evictions']:,} "
                f"({pct(summary['cross_evictions'], summary['total_evictions']):.2f}%)"
            ),
            (
                f"useful cross-workload evictions:     {summary['useful_cross_evictions']:,} "
                f"({pct(summary['useful_cross_evictions'], summary['total_useful']):.2f}% of useful)"
            ),
            f"chat evicted by rag:                 {summary['chat_rag_total']:,}",
            (
                f"useful chat evicted by rag:          {summary['chat_rag_useful']:,} "
                f"({summary['chat_rag_useful_rate']:.2f}% of chat<-rag)"
            ),
            f"RAG share of useful Chat evictions:  {summary['rag_share_of_useful_chat_evictions']:.2f}%",
        ]
    )


def format_pair_table(summary: dict[str, Any]) -> str:
    total_by_pair = summary["total_by_pair"]
    useful_by_pair = summary["useful_by_pair"]
    pairs = sorted(set(total_by_pair) | set(useful_by_pair))

    lines = [
        "",
        "Eviction attribution by pair",
        f"{'evicted':>10} {'trigger':>10} {'total':>10} {'useful':>10} {'useful%':>10}",
        "-" * 56,
    ]
    for evicted, trigger in pairs:
        total = total_by_pair[(evicted, trigger)]
        useful = useful_by_pair[(evicted, trigger)]
        lines.append(f"{evicted:>10} {trigger:>10} {total:>10} {useful:>10} {pct(useful, total):>9.2f}%")
    return "\n".join(lines)


def write_line(output: TextIO | None, text: str) -> None:
    print(text)
    if output is not None:
        output.write(text + "\n")


def summarize_files(paths: list[tuple[str, Path]], output: TextIO | None = None) -> None:
    for label, path in paths:
        rows = load_jsonl(path)
        write_line(output, "\n" + "=" * 72)
        write_line(output, label)
        write_line(output, str(path))
        write_line(output, "=" * 72)
        if not rows:
            write_line(output, f"missing or empty: {path}")
            continue
        summary = summarize_evictions(rows)
        write_line(output, format_key_metrics(summary))
        write_line(output, format_pair_table(summary))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print useful KV-cache eviction attribution summaries.")
    parser.add_argument("eviction_jsonl", nargs="*", type=Path, help="Optional eviction JSONL files.")
    parser.add_argument("--results-dir", type=Path, default=Path("../../results"))
    parser.add_argument("--output", type=Path, help="Optional text output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.eviction_jsonl:
        paths = [(path.stem, path.expanduser().resolve()) for path in args.eviction_jsonl]
    else:
        results_dir = args.results_dir.expanduser().resolve()
        paths = [(label, results_dir / filename) for label, filename in DEFAULT_RUNS]

    output_file = None
    try:
        if args.output:
            output_path = args.output.expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_file = output_path.open("w", encoding="utf-8")
        summarize_files(paths, output=output_file)
    finally:
        if output_file is not None:
            output_file.close()


if __name__ == "__main__":
    main()

"""Print turn-level Chat SLO / TTFT summaries from run JSONL files."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, TextIO


DEFAULT_RUNS = [
    ("isolated", "chat_isolated_turn9_qps5.jsonl"),
    ("ctx500", "mixed_chat5_rag5_longctx500_util06.jsonl"),
    ("ctx1500", "mixed_chat5_rag5_longctx1500_util06.jsonl"),
    ("ctx3000", "mixed_chat5_rag5_longctx3000_util06.jsonl"),
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


def percentile(values: list[float], q: float) -> float:
    clean = [value for value in values if value is not None and not math.isnan(value)]
    if not clean:
        return 0.0
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


def summarize_turns(rows: list[dict[str, Any]], slo_s: float) -> list[dict[str, Any]]:
    by_turn: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        if row.get("error"):
            continue
        workload = row.get("workload", "chat")
        if workload != "chat":
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
                "turn": turn_id,
                "count": len(turn_rows),
                "hit_rate": (total_cached / total_prompt) if total_prompt else 0.0,
                "ttft_p50_s": percentile(ttfts, 50),
                "ttft_p95_s": percentile(ttfts, 95),
                "slo_attainment": (sum(1 for ttft in ttfts if ttft <= slo_s) / len(turn_rows)) if turn_rows else 0.0,
            }
        )
    return summary


def format_table(label: str, summary: list[dict[str, Any]]) -> str:
    lines = [
        "",
        f"== {label} ==",
        f"{'turn':>4} {'count':>6} {'hit%':>8} {'p50_s':>8} {'p95_s':>8} {'SLO%':>8}",
        "-" * 50,
    ]
    for row in summary:
        lines.append(
            f"{row['turn']:>4} "
            f"{row['count']:>6} "
            f"{row['hit_rate'] * 100:>7.2f}% "
            f"{row['ttft_p50_s']:>8.3f} "
            f"{row['ttft_p95_s']:>8.3f} "
            f"{row['slo_attainment'] * 100:>7.2f}%"
        )
    return "\n".join(lines)


def write_line(output: TextIO | None, text: str) -> None:
    print(text)
    if output is not None:
        output.write(text + "\n")


def summarize_files(paths: list[tuple[str, Path]], slo_s: float, output: TextIO | None = None) -> None:
    for label, path in paths:
        rows = load_jsonl(path)
        if not rows:
            write_line(output, f"\n== {label} ==\nmissing or empty: {path}")
            continue
        write_line(output, format_table(label, summarize_turns(rows, slo_s=slo_s)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print turn-level Chat SLO / TTFT summaries.")
    parser.add_argument("jsonl", nargs="*", type=Path, help="Optional run JSONL files. Defaults to standard experiment files.")
    parser.add_argument("--results-dir", type=Path, default=Path("../../results"))
    parser.add_argument("--slo-s", type=float, default=0.5, help="Chat TTFT SLO threshold in seconds.")
    parser.add_argument("--output", type=Path, help="Optional text output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.jsonl:
        paths = [(path.stem, path.expanduser().resolve()) for path in args.jsonl]
    else:
        results_dir = args.results_dir.expanduser().resolve()
        paths = [(label, results_dir / filename) for label, filename in DEFAULT_RUNS]

    output_file = None
    try:
        if args.output:
            output_path = args.output.expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_file = output_path.open("w", encoding="utf-8")
        summarize_files(paths, slo_s=args.slo_s, output=output_file)
    finally:
        if output_file is not None:
            output_file.close()


if __name__ == "__main__":
    main()

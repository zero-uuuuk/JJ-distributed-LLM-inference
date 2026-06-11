from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "meta-llama/Llama-3.2-3B-Instruct"
AGENT_VALIDATION_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_VALIDATION_DIR.parent.parent
DEFAULT_TRACE = REPO_ROOT / "workloads/traj/traj_agent_100session_10step.jsonl"
DEFAULT_OUTPUT_DIR = AGENT_VALIDATION_DIR / "token_analysis"


class ApproxTokenizer:
    def count(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4)


class HFTokenizer:
    def __init__(self, model: str) -> None:
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(model)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        try:
            tokens = self.tokenizer.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
            )
            return len(tokens)
        except Exception:
            return len(self.tokenizer.encode(render_messages(messages), add_special_tokens=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot prompt-token distributions for trajectory agent traces.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--tokenizer",
        default="approx",
        help="Use 'approx' for char/4 token estimates, or a Hugging Face tokenizer name.",
    )
    parser.add_argument("--workload-label", default="Agent trajectory")
    parser.add_argument("--max-requests", type=int, default=0)
    return parser.parse_args()


def load_jsonl(path: Path, max_requests: int) -> list[dict[str, Any]]:
    resolved_path = path.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    with resolved_path.open("r", encoding="utf-8") as input_file:
        for line in input_file:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
            if max_requests > 0 and len(rows) >= max_requests:
                break
    return rows


def render_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", ""))
        parts.append(f"<|start_header_id|>{role}<|end_header_id|>\n\n{content}<|eot_id|>")
    parts.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
    return "".join(parts)


def load_counter(tokenizer_name: str, model: str) -> Any:
    if tokenizer_name.lower() in {"approx", "none"}:
        return ApproxTokenizer()
    return HFTokenizer(tokenizer_name or model)


def count_prompt_tokens(row: dict[str, Any], counter: Any) -> int:
    messages = row.get("messages")
    if not isinstance(messages, list):
        prompt = str(row.get("prompt", ""))
        return counter.count(prompt) if hasattr(counter, "count") else len(prompt)
    if isinstance(counter, HFTokenizer):
        return counter.count_messages(messages)
    return counter.count(render_messages(messages))


def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = (len(sorted_values) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(sorted_values[lower])
    weight = index - lower
    return float(sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight)


def summarize(values: list[int]) -> dict[str, float | int]:
    if not values:
        return {
            "n": 0,
            "sum": 0,
            "mean": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0,
        }
    return {
        "n": len(values),
        "sum": int(sum(values)),
        "mean": float(sum(values) / len(values)),
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": int(max(values)),
    }


def grouped_by_step(rows: list[dict[str, Any]], token_counts: list[int]) -> dict[int, list[int]]:
    groups: dict[int, list[int]] = {}
    for row, token_count in zip(rows, token_counts):
        step_id = int(row.get("step_id") or 0)
        groups.setdefault(step_id, []).append(token_count)
    return dict(sorted(groups.items()))


def configure_matplotlib() -> Any:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "axes.grid": True,
            "grid.alpha": 0.35,
            "font.size": 11,
            "axes.titlesize": 20,
            "axes.labelsize": 13,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
        }
    )
    return plt


def save_distribution_plot(
    plt: Any,
    values: list[int],
    stats: dict[str, float | int],
    label: str,
    output_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(16, 8), dpi=120)
    box = ax.boxplot(
        [values],
        tick_labels=[label],
        patch_artist=True,
        widths=0.45,
        showfliers=False,
    )
    box["boxes"][0].set_facecolor("#7aa0c7")
    box["boxes"][0].set_alpha(0.9)
    box["medians"][0].set_color("#d95f02")
    box["medians"][0].set_linewidth(2)
    ax.set_title("Prompt Token Distribution by Workload")
    ax.set_ylabel("Prompt tokens per request")
    ax.text(
        0.80,
        0.88,
        f"mean {stats['mean']:.0f}\np95 {stats['p95']:.0f}",
        transform=ax.transAxes,
        fontsize=13,
        ha="left",
        va="top",
    )
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


def save_total_plot(
    plt: Any,
    stats: dict[str, float | int],
    label: str,
    output_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(16, 8), dpi=120)
    total = int(stats["sum"])
    ax.bar([label], [total], color="#5fa357", alpha=0.95)
    ax.set_title("Total Prompt Tokens by Workload")
    ax.set_ylabel("Total prompt tokens")
    ax.ticklabel_format(axis="y", style="plain")
    ax.text(0, total * 1.02, f"{total:,}", ha="center", va="bottom", fontsize=15)
    ax.set_ylim(0, total * 1.18 if total > 0 else 1)
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


def save_step_plot(
    plt: Any,
    step_groups: dict[int, list[int]],
    output_path: Path,
) -> None:
    labels = [str(step_id) for step_id in step_groups]
    values = [step_groups[step_id] for step_id in step_groups]
    fig, ax = plt.subplots(figsize=(16, 8), dpi=120)
    box = ax.boxplot(values, tick_labels=labels, patch_artist=True, showfliers=False)
    for patch in box["boxes"]:
        patch.set_facecolor("#7aa0c7")
        patch.set_alpha(0.85)
    for median in box["medians"]:
        median.set_color("#d95f02")
        median.set_linewidth(2)
    ax.set_title("Agent Prompt Token Distribution by Step")
    ax.set_xlabel("Agent step")
    ax.set_ylabel("Prompt tokens per request")
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as output_file:
        json.dump(data, output_file, ensure_ascii=False, indent=2)


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = load_jsonl(args.trace, args.max_requests)
    counter = load_counter(args.tokenizer, args.model)
    prompt_tokens = [count_prompt_tokens(row, counter) for row in rows]
    stats = summarize(prompt_tokens)
    step_groups = grouped_by_step(rows, prompt_tokens)
    step_stats = {str(step_id): summarize(values) for step_id, values in step_groups.items()}

    plt = configure_matplotlib()
    save_distribution_plot(
        plt,
        prompt_tokens,
        stats,
        args.workload_label,
        output_dir / "prompt_token_distribution.png",
    )
    save_total_plot(
        plt,
        stats,
        args.workload_label,
        output_dir / "total_prompt_tokens.png",
    )
    save_step_plot(
        plt,
        step_groups,
        output_dir / "agent_step_prompt_tokens.png",
    )

    summary = {
        "trace": str(args.trace),
        "model": args.model,
        "tokenizer": args.tokenizer,
        "workload_label": args.workload_label,
        "stats": {args.workload_label: stats},
        "step_stats": step_stats,
        "outputs": {
            "prompt_token_distribution": str(output_dir / "prompt_token_distribution.png"),
            "total_prompt_tokens": str(output_dir / "total_prompt_tokens.png"),
            "agent_step_prompt_tokens": str(output_dir / "agent_step_prompt_tokens.png"),
        },
    }
    write_json(output_dir / "prompt_token_summary.json", summary)

    print(f"requests: {stats['n']}")
    print(f"total_prompt_tokens: {int(stats['sum']):,}")
    print(f"mean/p50/p95: {stats['mean']:.1f}/{stats['p50']:.1f}/{stats['p95']:.1f}")
    print(f"output_dir: {output_dir}")


if __name__ == "__main__":
    main()

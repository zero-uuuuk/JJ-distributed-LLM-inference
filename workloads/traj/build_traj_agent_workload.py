"""Build a Terminal-Bench trajectory based multi-step agent workload.

The input dataset is yoonholee/terminalbench-trajectories. Each dataset row is
one agent trial, and the JSON-serialized ``steps`` field contains user/system
messages, agent messages, tool calls, and observations. This builder turns one
trial into one agent session and emits one request per selected agent step.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator


DEFAULT_DATASET_ID = "yoonholee/terminalbench-trajectories"
DEFAULT_CONFIG = "default"
DEFAULT_SPLIT = "train"
DEFAULT_TOKENIZER = "meta-llama/Llama-3.2-3B-Instruct"

SYSTEM_PROMPT = """You are a terminal coding agent.
Continue the task using the available terminal tools when useful.
For this workload trace, produce the next agent step only."""

NEXT_ACTION_PROMPT = "Continue with the next terminal-agent action."


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a Terminal-Bench trajectory agent JSONL workload trace.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dataset-id", default=DEFAULT_DATASET_ID)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument(
        "--source-jsonl",
        type=Path,
        default=None,
        help="Optional local JSONL export with Terminal-Bench trajectory rows.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--num-sessions", type=int, default=100)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument(
        "--min-steps",
        type=int,
        default=None,
        help="Minimum selected agent steps required per session. Defaults to --max-steps.",
    )
    parser.add_argument(
        "--order",
        choices=("step-major", "session-major"),
        default="step-major",
        help="JSONL row order. Runtime scheduling should still use session semantics.",
    )
    parser.add_argument(
        "--allow-agent-steps-without-tools",
        action="store_true",
        help="Include agent steps that do not contain tool calls.",
    )
    parser.add_argument(
        "--keep-warmup",
        action="store_true",
        help="Keep leading warmup/user-ready messages in prompts.",
    )
    parser.add_argument(
        "--reward",
        choices=("any", "0", "1"),
        default="any",
        help="Filter by Terminal-Bench reward.",
    )
    parser.add_argument("--agent", default=None, help="Filter by agent scaffold.")
    parser.add_argument("--model", default=None, help="Filter by source model string.")
    parser.add_argument("--task-name", default=None, help="Filter by task name.")
    parser.add_argument(
        "--max-scan-rows",
        type=int,
        default=0,
        help="Maximum dataset rows to scan before stopping. 0 means no limit.",
    )
    parser.add_argument(
        "--streaming",
        action="store_true",
        help="Use Hugging Face streaming mode instead of downloading the split locally.",
    )
    parser.add_argument(
        "--shuffle-seed",
        type=int,
        default=42,
        help="Shuffle source rows before selecting sessions. Use a negative value to disable.",
    )
    parser.add_argument(
        "--shuffle-buffer",
        type=int,
        default=10_000,
        help="Buffer size for streaming shuffle.",
    )
    parser.add_argument(
        "--max-step-msg-chars",
        type=int,
        default=4_000,
        help="Maximum characters kept from each step message. 0 means no limit.",
    )
    parser.add_argument(
        "--max-observation-chars",
        type=int,
        default=2_000,
        help="Maximum characters kept from each observation. 0 means no limit.",
    )
    parser.add_argument(
        "--tokenizer",
        default=DEFAULT_TOKENIZER,
        help="Tokenizer for output_token_len. Use 'approx' to avoid transformers.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=0,
        help="Clamp output_token_len to this value. 0 means no clamp.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# IO and dataset loading
# ---------------------------------------------------------------------------


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    resolved_path = path.expanduser().resolve()
    with resolved_path.open("r", encoding="utf-8") as input_file:
        for line in input_file:
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_hf_rows(args: argparse.Namespace) -> Iterable[dict[str, Any]]:
    if args.source_jsonl is not None:
        return read_jsonl(args.source_jsonl)

    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: datasets. Install it in the active venv with "
            "`pip install datasets pyarrow`, or pass --source-jsonl."
        ) from exc

    dataset = load_dataset(
        args.dataset_id,
        args.config,
        split=args.split,
        streaming=args.streaming,
    )
    if args.shuffle_seed >= 0:
        if args.streaming:
            dataset = dataset.shuffle(
                seed=args.shuffle_seed,
                buffer_size=max(1, args.shuffle_buffer),
            )
        else:
            dataset = dataset.shuffle(seed=args.shuffle_seed)
    return dataset


# ---------------------------------------------------------------------------
# Text shaping
# ---------------------------------------------------------------------------


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def truncate_text(value: Any, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " [truncated]"


def sanitize_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "unknown"


def parse_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw_steps = row.get("steps")
    if raw_steps is None:
        return []
    if isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw_steps, list):
        return []
    return [step for step in raw_steps if isinstance(step, dict)]


def has_tools(step: dict[str, Any]) -> bool:
    tools = step.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def is_leading_warmup_step(step: dict[str, Any]) -> bool:
    src = str(step.get("src", "")).lower()
    msg = str(step.get("msg") or "").strip().lower()
    if src == "user" and msg == "warmup":
        return True
    if src == "agent" and not has_tools(step):
        ready_words = ("ready", "prepared", "help", "assist")
        return any(word in msg for word in ready_words) and len(msg) < 2_000
    return False


def strip_leading_warmup(steps: list[dict[str, Any]], keep_warmup: bool) -> list[dict[str, Any]]:
    if keep_warmup:
        return steps
    index = 0
    while index < len(steps) and is_leading_warmup_step(steps[index]):
        index += 1
    return steps[index:]


def step_to_assistant_text(
    step: dict[str, Any],
    max_step_msg_chars: int,
) -> str:
    parts: list[str] = []
    msg = truncate_text(step.get("msg"), max_step_msg_chars).strip()
    if msg:
        parts.append(msg)
    if has_tools(step):
        parts.append("[Tool calls]\n" + compact_json(step.get("tools")))
    if not parts:
        parts.append(compact_json(step))
    return "\n\n".join(parts)


def step_to_history_messages(
    step: dict[str, Any],
    max_step_msg_chars: int,
    max_observation_chars: int,
) -> list[dict[str, str]]:
    src = str(step.get("src", "user")).lower()
    if src == "agent":
        messages = [
            {
                "role": "assistant",
                "content": step_to_assistant_text(step, max_step_msg_chars),
            }
        ]
        obs = truncate_text(step.get("obs"), max_observation_chars).strip()
        if obs:
            messages.append({"role": "user", "content": f"Observation:\n{obs}"})
        return messages

    role = "system" if src == "system" else "user"
    msg = truncate_text(step.get("msg"), max_step_msg_chars).strip()
    obs = truncate_text(step.get("obs"), max_observation_chars).strip()
    content_parts = []
    if msg:
        content_parts.append(msg)
    if obs:
        content_parts.append("Observation:\n" + obs)
    if not content_parts:
        content_parts.append(compact_json(step))
    return [{"role": role, "content": "\n\n".join(content_parts)}]


def metadata_prompt(row: dict[str, Any]) -> str:
    fields = [
        ("task_name", row.get("task_name")),
        ("trial_name", row.get("trial_name")),
        ("trial_id", row.get("trial_id")),
        ("agent", row.get("agent")),
        ("source_model", row.get("model")),
        ("reward", row.get("reward")),
    ]
    lines = ["[Terminal-Bench trajectory metadata]"]
    for key, value in fields:
        if value is not None and str(value).strip():
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def selected_agent_indices(
    steps: list[dict[str, Any]],
    allow_without_tools: bool,
) -> list[int]:
    indices: list[int] = []
    for index, step in enumerate(steps):
        if str(step.get("src", "")).lower() != "agent":
            continue
        if not allow_without_tools and not has_tools(step):
            continue
        indices.append(index)
    return indices


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


class ApproxTokenizer:
    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        approx_tokens = max(1, (len(text) + 3) // 4)
        return list(range(approx_tokens))


def load_tokenizer(tokenizer_name: str) -> Any:
    if tokenizer_name.lower() in {"approx", "none"}:
        return ApproxTokenizer()

    try:
        from transformers import AutoTokenizer
    except ImportError:
        print(
            "warning: transformers is not installed; using approximate token counts",
            file=sys.stderr,
        )
        return ApproxTokenizer()

    try:
        return AutoTokenizer.from_pretrained(tokenizer_name)
    except Exception as exc:
        raise SystemExit(
            f"Failed to load tokenizer '{tokenizer_name}': {exc}\n"
            "Use --tokenizer approx to build a planning trace without transformers."
        ) from exc


def count_output_tokens(tokenizer: Any, text: str, max_output_tokens: int) -> int:
    token_count = len(tokenizer.encode(text, add_special_tokens=False))
    if max_output_tokens > 0:
        return min(token_count, max_output_tokens)
    return token_count


# ---------------------------------------------------------------------------
# Workload construction
# ---------------------------------------------------------------------------


def row_matches_filters(row: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.reward != "any" and str(row.get("reward")) != args.reward:
        return False
    if args.agent is not None and str(row.get("agent")) != args.agent:
        return False
    if args.model is not None and str(row.get("model")) != args.model:
        return False
    if args.task_name is not None and str(row.get("task_name")) != args.task_name:
        return False
    return True


def select_sessions(
    rows: Iterable[dict[str, Any]],
    args: argparse.Namespace,
    min_steps: int,
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]], list[int]]], dict[str, int]]:
    selected: list[tuple[dict[str, Any], list[dict[str, Any]], list[int]]] = []
    stats = {
        "scanned_rows": 0,
        "filtered_rows": 0,
        "missing_steps_rows": 0,
        "too_few_steps_rows": 0,
    }

    for row in rows:
        stats["scanned_rows"] += 1
        if args.max_scan_rows > 0 and stats["scanned_rows"] > args.max_scan_rows:
            break
        if not row_matches_filters(row, args):
            stats["filtered_rows"] += 1
            continue

        steps = strip_leading_warmup(parse_steps(row), args.keep_warmup)
        if not steps:
            stats["missing_steps_rows"] += 1
            continue

        agent_indices = selected_agent_indices(
            steps=steps,
            allow_without_tools=args.allow_agent_steps_without_tools,
        )
        if len(agent_indices) < min_steps:
            stats["too_few_steps_rows"] += 1
            continue

        selected.append((row, steps, agent_indices[: args.max_steps]))
        if args.num_sessions > 0 and len(selected) >= args.num_sessions:
            break

    if args.num_sessions > 0 and len(selected) < args.num_sessions:
        raise SystemExit(
            f"Only found {len(selected)} trajectories with at least {min_steps} "
            f"selected agent steps; requested {args.num_sessions} sessions. "
            "Lower --num-sessions/--min-steps, allow no-tool steps, or relax filters."
        )

    return selected, stats


def first_tool_name(step: dict[str, Any]) -> str | None:
    tools = step.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    first_tool = tools[0]
    if isinstance(first_tool, dict):
        return str(first_tool.get("fn") or first_tool.get("name") or "tool")
    return "tool"


def first_tool_arguments(step: dict[str, Any]) -> Any:
    tools = step.get("tools")
    if not isinstance(tools, list) or not tools:
        return {}
    first_tool = tools[0]
    if isinstance(first_tool, dict):
        return {key: value for key, value in first_tool.items() if key not in {"fn", "name"}}
    return first_tool


def build_session_rows(
    row: dict[str, Any],
    steps: list[dict[str, Any]],
    agent_indices: list[int],
    tokenizer: Any,
    max_output_tokens: int,
    max_step_msg_chars: int,
    max_observation_chars: int,
) -> list[dict[str, Any]]:
    trial_id = sanitize_id(row.get("trial_id") or row.get("trial_name") or row.get("task_name"))
    session_id = f"traj-{trial_id}"
    session_rows: list[dict[str, Any]] = []

    for step_number, source_step_index in enumerate(agent_indices, start=1):
        step = steps[source_step_index]
        output_text = step_to_assistant_text(step, max_step_msg_chars)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": metadata_prompt(row)},
        ]
        for history_step in steps[:source_step_index]:
            messages.extend(
                step_to_history_messages(
                    history_step,
                    max_step_msg_chars=max_step_msg_chars,
                    max_observation_chars=max_observation_chars,
                )
            )
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": NEXT_ACTION_PROMPT})

        session_rows.append(
            {
                "request_id": f"{session_id}_step_{step_number}",
                "session_id": session_id,
                "task_id": row.get("task_name"),
                "trial_id": row.get("trial_id"),
                "trial_name": row.get("trial_name"),
                "agent_scaffold": row.get("agent"),
                "source_model": row.get("model"),
                "reward": row.get("reward"),
                "step_id": step_number,
                "session_step_count": len(agent_indices),
                "source_step_index": source_step_index,
                "messages": messages,
                "output_text": output_text,
                "output_token_len": count_output_tokens(
                    tokenizer, output_text, max_output_tokens
                ),
                "tool_name": first_tool_name(step),
                "tool_arguments": first_tool_arguments(step),
                "tool_gap_seconds": None,
                "source_dataset": DEFAULT_DATASET_ID,
                "cache_pattern": "agent_multi_step",
            }
        )

    return session_rows


def flatten_sessions(
    session_rows: list[list[dict[str, Any]]],
    order: str,
) -> list[dict[str, Any]]:
    if order == "session-major":
        return [row for rows in session_rows for row in rows]

    max_steps = max((len(rows) for rows in session_rows), default=0)
    rows: list[dict[str, Any]] = []
    for step_offset in range(max_steps):
        for session in session_rows:
            if step_offset < len(session):
                rows.append(session[step_offset])
    return rows


def build_rows(args: argparse.Namespace, min_steps: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    tokenizer = load_tokenizer(args.tokenizer)
    source_rows = load_hf_rows(args)
    selected, stats = select_sessions(source_rows, args, min_steps)
    all_session_rows = [
        build_session_rows(
            row=row,
            steps=steps,
            agent_indices=agent_indices,
            tokenizer=tokenizer,
            max_output_tokens=args.max_output_tokens,
            max_step_msg_chars=args.max_step_msg_chars,
            max_observation_chars=args.max_observation_chars,
        )
        for row, steps, agent_indices in selected
    ]
    return flatten_sessions(all_session_rows, args.order), stats


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    if args.max_steps < 1:
        raise SystemExit("--max-steps must be at least 1")
    min_steps = args.min_steps if args.min_steps is not None else args.max_steps
    if min_steps < 1:
        raise SystemExit("--min-steps must be at least 1")
    if min_steps > args.max_steps:
        raise SystemExit("--min-steps cannot be greater than --max-steps")
    if args.num_sessions < 0:
        raise SystemExit("--num-sessions must be >= 0")

    print(f"dataset: {args.dataset_id}", flush=True)
    print(f"config: {args.config}", flush=True)
    print(f"split: {args.split}", flush=True)
    print(f"streaming: {args.streaming}", flush=True)
    print(f"tokenizer: {args.tokenizer}", flush=True)

    rows, stats = build_rows(args, min_steps)
    write_jsonl(args.output, rows)

    output_lens = [row["output_token_len"] for row in rows]
    prompt_lens = [sum(len(message["content"]) for message in row["messages"]) for row in rows]
    sessions = {row["session_id"] for row in rows}
    requests = len(rows)
    avg_steps = requests / len(sessions) if sessions else 0.0

    print(f"scanned_rows: {stats['scanned_rows']}")
    print(f"filtered_rows: {stats['filtered_rows']}")
    print(f"missing_steps_rows: {stats['missing_steps_rows']}")
    print(f"too_few_steps_rows: {stats['too_few_steps_rows']}")
    print(f"sessions: {len(sessions)}")
    print(f"requests: {requests}")
    print(f"avg_steps_per_session: {avg_steps:.2f}")
    print(f"order: {args.order}")
    print(f"min_steps: {min_steps}")
    print(f"max_steps: {args.max_steps}")
    print(
        "output_token_len min/avg/max: "
        f"{min(output_lens) if output_lens else 0}/"
        f"{sum(output_lens) / len(output_lens) if output_lens else 0:.1f}/"
        f"{max(output_lens) if output_lens else 0}"
    )
    print(
        "prompt_char_len min/avg/max: "
        f"{min(prompt_lens) if prompt_lens else 0}/"
        f"{sum(prompt_lens) / len(prompt_lens) if prompt_lens else 0:.1f}/"
        f"{max(prompt_lens) if prompt_lens else 0}"
    )
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()

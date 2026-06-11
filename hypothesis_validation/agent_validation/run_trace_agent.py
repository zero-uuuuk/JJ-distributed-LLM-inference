from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

from run_mixed_agent import (
    AGENT_VALIDATION_DIR,
    DEFAULT_AGENT_SLO_MS,
    DEFAULT_AGENT_TRACE,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_URL,
    build_summary,
    load_jsonl,
    load_runtime_dependencies,
    print_summary,
    resolve_gap_mode,
    resolve_summary_path,
    resolve_trace_path,
    run_agent_workload,
    write_json,
    write_jsonl,
)


DEFAULT_OUTPUT_DIR = AGENT_VALIDATION_DIR / "raw_results"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run trajectory Agent-only validation with completion-based tool gaps.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--trace",
        "--agent-trace",
        dest="agent_trace",
        type=Path,
        default=DEFAULT_AGENT_TRACE,
    )
    parser.add_argument(
        "--phase",
        choices=("agent_only_fixed", "agent_only_gaussian", "agent_only_exponential"),
        default="agent_only_fixed",
    )
    parser.add_argument("--agent-target-rps", type=float, default=5.0)
    parser.add_argument("--agent-steps-per-session", type=float, default=10.0)
    parser.add_argument(
        "--agent-tool-gap-mode",
        choices=("fixed", "gaussian", "exponential"),
        default=None,
        help="Defaults from --phase if omitted.",
    )
    parser.add_argument("--agent-tool-gap-seconds", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-mean", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-std", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-min", type=float, default=0.0)
    parser.add_argument("--agent-tool-gap-max", type=float, default=20.0)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-concurrency", type=int, default=32)
    parser.add_argument("--num-prompts", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--slo-ms", type=float, default=DEFAULT_AGENT_SLO_MS)
    return parser.parse_args()


def resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output is not None:
        return args.output.expanduser().resolve()
    return (DEFAULT_OUTPUT_DIR / f"{args.phase}.jsonl").resolve()


def build_agent_only_summary(
    results: list[dict[str, Any]],
    duration_seconds: float,
    phase: str,
    slo_ms: float,
    scheduler: dict[str, Any],
) -> dict[str, Any]:
    summary = build_summary(
        results=results,
        duration_seconds=duration_seconds,
        phase=phase,
        agent_slo_ms=slo_ms,
        chat_slo_ms=None,
        scheduler=scheduler,
    )
    summary.pop("chat", None)
    return summary


async def main_async(args: argparse.Namespace) -> None:
    aiohttp, tqdm = load_runtime_dependencies()

    gap_mode = resolve_gap_mode(args.phase, args.agent_tool_gap_mode)
    agent_trace = resolve_trace_path(
        args.agent_trace,
        "Agent",
        "  cd workloads/traj && python build_traj_agent_workload.py "
        "--num-sessions 100 --max-steps 10 --min-steps 10 "
        "--output traj_agent_100session_10step.jsonl --tokenizer approx",
    )
    output_path = resolve_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    agent_requests = load_jsonl(agent_trace)
    if args.num_prompts > 0:
        agent_requests = agent_requests[: args.num_prompts]

    scheduler = {
        "agent_target_rps": args.agent_target_rps,
        "agent_steps_per_session": args.agent_steps_per_session,
        "agent_session_start_rps": (
            args.agent_target_rps / args.agent_steps_per_session
            if args.agent_target_rps > 0 and args.agent_steps_per_session > 0
            else 0.0
        ),
        "agent_tool_gap_mode": gap_mode,
        "agent_tool_gap_seconds": args.agent_tool_gap_seconds,
        "agent_tool_gap_mean": args.agent_tool_gap_mean,
        "agent_tool_gap_std": args.agent_tool_gap_std,
        "agent_tool_gap_min": args.agent_tool_gap_min,
        "agent_tool_gap_max": args.agent_tool_gap_max,
    }

    print(
        f"phase={args.phase} | agent={len(agent_requests)} "
        f"@ target {args.agent_target_rps} rps | "
        f"concurrency={args.max_concurrency} | url={args.url}"
    )
    print(f"agent_trace={agent_trace}")
    print(f"gap_mode={gap_mode}")
    print(f"output={output_path}")

    semaphore = asyncio.Semaphore(args.max_concurrency)
    timeout = aiohttp.ClientTimeout(total=args.timeout_seconds)
    results: list[dict[str, Any]] = []
    progress_lock = asyncio.Lock()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        start_perf = time.perf_counter()
        with tqdm(total=len(agent_requests), unit="req", dynamic_ncols=True) as progress_bar:
            await run_agent_workload(
                agent_requests=agent_requests,
                agent_target_rps=args.agent_target_rps,
                agent_steps_per_session=args.agent_steps_per_session,
                session=session,
                url=args.url,
                model=args.model,
                semaphore=semaphore,
                results=results,
                progress_bar=progress_bar,
                progress_lock=progress_lock,
                phase=args.phase,
                gap_mode=gap_mode,
                fixed_gap_seconds=args.agent_tool_gap_seconds,
                gap_mean=args.agent_tool_gap_mean,
                gap_std=args.agent_tool_gap_std,
                gap_min=args.agent_tool_gap_min,
                gap_max=args.agent_tool_gap_max,
            )
        duration_seconds = time.perf_counter() - start_perf

    summary = build_agent_only_summary(
        results=results,
        duration_seconds=duration_seconds,
        phase=args.phase,
        slo_ms=args.slo_ms,
        scheduler=scheduler,
    )
    summary_path = resolve_summary_path(output_path)
    write_jsonl(output_path, results)
    write_json(summary_path, summary)
    print_summary(summary, ["agent"])
    print(f"\nsaved: {output_path}")
    print(f"summary: {summary_path}")


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

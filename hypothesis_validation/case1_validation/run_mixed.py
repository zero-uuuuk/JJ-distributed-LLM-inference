"""역할: Chat과 RAG/Longctx/Agent workload를 동시에 전송해 prefix KV cache pollution을 측정한다.

상세 과정:
  1. Chat은 Poisson 도착 과정으로 요청을 보낸다.
  2. RAG/Longctx는 독립 Poisson 도착 과정으로, Agent는 session completion 이후 tool gap 구조로 요청을 보낸다.
  3. 공통 streaming client로 TTFT·TPOT·cache hit rate를 기록한다.
  4. workload 태그별 summary와 scheduler 설정을 저장해 eviction attribution 분석과 연결한다.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

import aiohttp
from tqdm import tqdm

from run_trace import (
    DEFAULT_CHAT_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    build_agent_scheduler_summary,
    build_mixed_summary,
    load_jsonl,
    print_mixed_summary,
    resolve_summary_path,
    resolve_trace_path,
    run_agent_workload,
    run_poisson_workload,
    write_json,
    write_jsonl,
)


DEFAULT_CHAT_SLO_MS = 400.0
DEFAULT_RAG_SLO_MS = 400.0
DEFAULT_LONGCTX_SLO_MS = 7700.0
DEFAULT_AGENT_SLO_MS = 10000.0

CASE1_VALIDATION_DIR = Path(__file__).resolve().parent
HYPOTHESIS_DIR = CASE1_VALIDATION_DIR.parent
REPO_ROOT = HYPOTHESIS_DIR.parent
DEFAULT_CHAT_TRACE = REPO_ROOT / "workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl"
DEFAULT_RAG_TRACE = REPO_ROOT / "workloads/msmarco/msmarco_v21_validation.jsonl"
DEFAULT_LONGCTX_TRACE = REPO_ROOT / "workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl"
DEFAULT_AGENT_TRACE = REPO_ROOT / "workloads/traj/traj_agent_100session_10step.jsonl"
DEFAULT_OUTPUT_DIR = CASE1_VALIDATION_DIR / "raw_results"


# ---------------------------------------------------------------------------
# CLI 처리
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chat + RAG/Longctx/Agent mixed workload를 동시에 전송합니다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "예시:\n"
            "  python hypothesis_validation/case1_validation/run_mixed.py --chat-qps 5 --rag-qps 5\n"
            "  python hypothesis_validation/case1_validation/run_mixed.py --longctx-trace\n"
            "  python hypothesis_validation/case1_validation/run_mixed.py --agent-trace "
            "workloads/traj/traj_agent_100session_10step.jsonl"
        ),
    )
    parser.add_argument("--chat-trace", type=Path, default=DEFAULT_CHAT_TRACE, help="ShareGPT Chat JSONL 경로")
    parser.add_argument("--rag-trace", type=Path, default=DEFAULT_RAG_TRACE, help="MS MARCO RAG JSONL 경로")
    parser.add_argument(
        "--longctx-trace",
        type=Path,
        nargs="?",
        const=DEFAULT_LONGCTX_TRACE,
        default=None,
        help="Longctx JSONL 경로. 값 없이 지정하면 기본 HotpotQA trace를 사용합니다.",
    )
    parser.add_argument(
        "--agent-trace",
        type=Path,
        nargs="?",
        const=DEFAULT_AGENT_TRACE,
        default=None,
        help="Agent JSONL 경로. 값 없이 지정하면 기본 Terminal-Bench trajectory trace를 사용합니다.",
    )
    parser.add_argument("--chat-qps", type=float, default=5.0, help="Chat 도착 QPS")
    parser.add_argument("--rag-qps", type=float, default=5.0, help="RAG 도착 QPS")
    parser.add_argument("--longctx-qps", type=float, default=5.0, help="Longctx 도착 QPS")
    parser.add_argument("--agent-target-rps", type=float, default=5.0, help="Agent 전체 target request rate")
    parser.add_argument("--agent-steps-per-session", type=float, default=10.0)
    parser.add_argument(
        "--agent-tool-gap-mode",
        choices=("fixed", "gaussian", "exponential"),
        default="exponential",
    )
    parser.add_argument("--agent-tool-gap-seconds", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-mean", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-std", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-min", type=float, default=0.0)
    parser.add_argument("--agent-tool-gap-max", type=float, default=20.0)
    parser.add_argument("--output", type=Path, default=None, help="결과 JSONL 저장 경로")
    parser.add_argument("--url", default=DEFAULT_CHAT_URL, help="vLLM chat completions URL")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-concurrency", type=int, default=32)
    parser.add_argument("--num-chat-prompts", type=int, default=1000)
    parser.add_argument("--num-rag-prompts", type=int, default=1000)
    parser.add_argument("--num-longctx-prompts", type=int, default=1000)
    parser.add_argument("--num-agent-prompts", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--chat-slo-ms", type=float, default=DEFAULT_CHAT_SLO_MS)
    parser.add_argument("--rag-slo-ms", type=float, default=DEFAULT_RAG_SLO_MS)
    parser.add_argument("--longctx-slo-ms", type=float, default=DEFAULT_LONGCTX_SLO_MS)
    parser.add_argument("--agent-slo-ms", type=float, default=DEFAULT_AGENT_SLO_MS)
    return parser.parse_args()


def format_qps_label(qps: float) -> str:
    """파일명에 넣을 QPS 값을 짧고 안전한 문자열로 변환한다."""
    if float(qps).is_integer():
        return str(int(qps))
    return str(qps).replace("-", "m").replace(".", "p")


def resolve_output_path(args: argparse.Namespace, antagonist_tag: str, antagonist_rate: float) -> Path:
    """명시된 output이 없으면 workload 종류가 드러나는 기본 파일명을 만든다."""
    if args.output is not None:
        return args.output.expanduser().resolve()

    chat_label = format_qps_label(args.chat_qps)
    antagonist_label = format_qps_label(antagonist_rate)
    model_len_label = "len12288" if antagonist_tag == "agent" else "len8192"
    filename = f"mixed_chat{chat_label}_{antagonist_tag}{antagonist_label}_apc_on_{model_len_label}.jsonl"
    return (DEFAULT_OUTPUT_DIR / filename).resolve()


def resolve_antagonist_config(args: argparse.Namespace) -> dict[str, Any]:
    """두 번째 workload를 RAG, Longctx, Agent 중 하나로 결정한다."""
    if args.longctx_trace is not None and args.agent_trace is not None:
        raise SystemExit("--longctx-trace와 --agent-trace는 동시에 지정할 수 없습니다.")

    if args.agent_trace is not None:
        return {
            "tag": "agent",
            "label": "Agent",
            "trace": args.agent_trace,
            "rate": args.agent_target_rps,
            "num_prompts": args.num_agent_prompts,
            "slo_ms": args.agent_slo_ms,
            "scheduler": "agent-session",
            "build_hint": (
                "  python workloads/traj/build_traj_agent_workload.py "
                "--num-sessions 100 --max-steps 10 --min-steps 10 "
                "--output workloads/traj/traj_agent_100session_10step.jsonl"
            ),
        }

    if args.longctx_trace is not None:
        return {
            "tag": "longctx",
            "label": "Longctx",
            "trace": args.longctx_trace,
            "rate": args.longctx_qps,
            "num_prompts": args.num_longctx_prompts,
            "slo_ms": args.longctx_slo_ms,
            "scheduler": "poisson",
            "build_hint": (
                "  python workloads/hotpotqa/build_hotpotqa_workload.py "
                "--output workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl"
            ),
        }

    return {
        "tag": "rag",
        "label": "RAG",
        "trace": args.rag_trace,
        "rate": args.rag_qps,
        "num_prompts": args.num_rag_prompts,
        "slo_ms": args.rag_slo_ms,
        "scheduler": "poisson",
        "build_hint": (
            "  python workloads/msmarco/build_msmarco_rag_workload.py "
            "--output workloads/msmarco/msmarco_v21_validation.jsonl"
        ),
    }


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    antagonist_config = resolve_antagonist_config(args)
    antagonist_tag = antagonist_config["tag"]
    antagonist_rate = float(antagonist_config["rate"])

    chat_trace = resolve_trace_path(
        args.chat_trace,
        "Chat",
        "  python workloads/sharegpt/build_sharegpt_workload.py "
        "--num-conversations 100 --min-turns 10 --max-turns 10 "
        "--order turn-major --output workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl",
    )
    antagonist_trace = resolve_trace_path(
        antagonist_config["trace"],
        antagonist_config["label"],
        antagonist_config["build_hint"],
    )
    output_path = resolve_output_path(args, antagonist_tag, antagonist_rate)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chat_requests = load_jsonl(chat_trace)
    antagonist_requests = load_jsonl(antagonist_trace)
    if args.num_chat_prompts > 0:
        chat_requests = chat_requests[: args.num_chat_prompts]
    if antagonist_config["num_prompts"] > 0:
        antagonist_requests = antagonist_requests[: antagonist_config["num_prompts"]]

    total = len(chat_requests) + len(antagonist_requests)
    scheduler: dict[str, Any] = {
        "chat": {"scheduler": "poisson", "qps": args.chat_qps},
    }
    if antagonist_tag == "agent":
        scheduler["agent"] = build_agent_scheduler_summary(
            target_rps=args.agent_target_rps,
            steps_per_session=args.agent_steps_per_session,
            gap_mode=args.agent_tool_gap_mode,
            gap_seconds=args.agent_tool_gap_seconds,
            gap_mean=args.agent_tool_gap_mean,
            gap_std=args.agent_tool_gap_std,
            gap_min=args.agent_tool_gap_min,
            gap_max=args.agent_tool_gap_max,
        )
    else:
        scheduler[antagonist_tag] = {"scheduler": "poisson", "qps": antagonist_rate}

    print(
        f"chat={len(chat_requests)} @ {args.chat_qps} qps | "
        f"{antagonist_tag}={len(antagonist_requests)} @ {antagonist_rate} rps | "
        f"total={total} | concurrency={args.max_concurrency} | url={args.url}"
    )
    print(f"chat_trace={chat_trace}")
    print(f"{antagonist_tag}_trace={antagonist_trace}")
    print(f"output={output_path}")

    semaphore = asyncio.Semaphore(args.max_concurrency)
    timeout = aiohttp.ClientTimeout(total=args.timeout_seconds)
    results: list[dict[str, Any]] = []
    progress_lock = asyncio.Lock()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        start_perf = time.perf_counter()
        with tqdm(total=total, unit="req", dynamic_ncols=True) as progress_bar:
            tasks = [
                run_poisson_workload(
                    requests=chat_requests,
                    qps=args.chat_qps,
                    workload_tag="chat",
                    seed=42,
                    session=session,
                    url=args.url,
                    model=args.model,
                    semaphore=semaphore,
                    results=results,
                    progress_bar=progress_bar,
                    progress_lock=progress_lock,
                    api="chat",
                )
            ]
            if antagonist_tag == "agent":
                tasks.append(
                    run_agent_workload(
                        agent_requests=antagonist_requests,
                        agent_target_rps=args.agent_target_rps,
                        agent_steps_per_session=args.agent_steps_per_session,
                        session=session,
                        url=args.url,
                        model=args.model,
                        semaphore=semaphore,
                        results=results,
                        progress_bar=progress_bar,
                        progress_lock=progress_lock,
                        gap_mode=args.agent_tool_gap_mode,
                        fixed_gap_seconds=args.agent_tool_gap_seconds,
                        gap_mean=args.agent_tool_gap_mean,
                        gap_std=args.agent_tool_gap_std,
                        gap_min=args.agent_tool_gap_min,
                        gap_max=args.agent_tool_gap_max,
                        api="chat",
                    )
                )
            else:
                tasks.append(
                    run_poisson_workload(
                        requests=antagonist_requests,
                        qps=antagonist_rate,
                        workload_tag=antagonist_tag,
                        seed=43,
                        session=session,
                        url=args.url,
                        model=args.model,
                        semaphore=semaphore,
                        results=results,
                        progress_bar=progress_bar,
                        progress_lock=progress_lock,
                        api="chat",
                    )
                )
            await asyncio.gather(*tasks)
        duration_seconds = time.perf_counter() - start_perf

    workload_tags = ["chat", antagonist_tag]
    summary = build_mixed_summary(
        results=results,
        duration_seconds=duration_seconds,
        workload_slos={"chat": args.chat_slo_ms, antagonist_tag: antagonist_config["slo_ms"]},
        scheduler=scheduler,
        antagonist_tag=antagonist_tag,
    )
    summary_path = resolve_summary_path(output_path)

    write_jsonl(output_path, results)
    write_json(summary_path, summary)
    print_mixed_summary(summary, workload_tags)
    print(f"\n저장 완료: {output_path}")
    print(f"요약 저장 완료: {summary_path}")


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

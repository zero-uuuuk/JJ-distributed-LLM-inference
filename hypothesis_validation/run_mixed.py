"""역할: Chat(ShareGPT)과 RAG(SQuAD) 두 워크로드를 동시에 vLLM 서버로 전송해
       prefix KV cache pollution 현상을 측정한다.

상세 과정:
  1. 두 워크로드 각각 독립적인 Poisson 도착 과정으로 공유 큐에 요청을 투입한다.
  2. 소비자 코루틴 풀이 큐에서 요청을 꺼내 vLLM OpenAI 호환 API로 전송한다.
  3. 각 요청의 TTFT·캐시 hit rate를 기록하고, SLO attainment를 집계한다.
  4. vLLM 내부 eviction 계측을 위해 OpenAI 'user' 필드로 workload 태그를 전달한다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Iterable

import aiohttp
import numpy as np
from tqdm import tqdm


DEFAULT_MODEL = "meta-llama/Llama-3.2-3B-Instruct"
DEFAULT_MAX_TOKENS = 128
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_CHAT_SLO_MS = 500.0
DEFAULT_RAG_SLO_MS = 2000.0
DEFAULT_URL = "http://127.0.0.1:8000/v1/chat/completions"

HYPOTHESIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HYPOTHESIS_DIR.parent
DEFAULT_CHAT_TRACE = REPO_ROOT / "workloads/sharegpt/sharegpt_conversation.jsonl"
DEFAULT_RAG_TRACE = REPO_ROOT / "workloads/squad/squad_validation.jsonl"
DEFAULT_OUTPUT = HYPOTHESIS_DIR / "results/mixed_5_5_apc_on_len8192.jsonl"


# ---------------------------------------------------------------------------
# JSONL 입출력
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved_path = path.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    with resolved_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI 처리
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chat + RAG 두 workload를 동시에 전송해 cache pollution을 측정합니다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "예시 (hypothesis_validation/ 에서):\n"
            "  python run_mixed.py\n"
            "  python run_mixed.py --chat-qps 5 --rag-qps 5 "
            "--output results/mixed_5_5_apc_on_len8192.jsonl"
        ),
    )
    parser.add_argument(
        "--chat-trace",
        type=Path,
        default=DEFAULT_CHAT_TRACE,
        help="ShareGPT JSONL 경로",
    )
    parser.add_argument(
        "--rag-trace",
        type=Path,
        default=DEFAULT_RAG_TRACE,
        help="RAG JSONL 경로 (기본: SQuAD validation trace)",
    )
    parser.add_argument("--chat-qps", type=float, default=5.0, help="Chat 도착 QPS")
    parser.add_argument("--rag-qps", type=float, default=5.0, help="RAG 도착 QPS")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="결과 JSONL 저장 경로",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="vLLM chat completions URL")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--max-concurrency", type=int, default=32)
    parser.add_argument("--num-chat-prompts", type=int, default=500)
    parser.add_argument("--num-rag-prompts", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--chat-slo-ms", type=float, default=DEFAULT_CHAT_SLO_MS, help="Chat TTFT SLO (ms)")
    parser.add_argument("--rag-slo-ms", type=float, default=DEFAULT_RAG_SLO_MS, help="RAG TTFT SLO (ms)")
    return parser.parse_args()


def resolve_trace_path(path: Path, label: str, build_hint: str) -> Path:
    """trace 경로를 절대 경로로 변환하고, 없으면 생성 방법을 안내한다."""
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        return resolved
    raise SystemExit(
        f"{label} trace를 찾을 수 없습니다: {resolved}\n"
        f"먼저 워크로드를 생성하세요:\n{build_hint}"
    )


# ---------------------------------------------------------------------------
# 통계 유틸리티
# ---------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    return float(np.percentile(values, p)) if values else float("nan")


def extract_cached_tokens(usage: dict[str, Any] | None) -> int | None:
    if not usage:
        return None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        for key in ("cached_tokens", "num_cached_tokens", "cache_tokens"):
            value = details.get(key)
            if isinstance(value, (int, float)):
                return int(value)
    for key in ("cached_tokens", "num_cached_tokens", "cache_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


# ---------------------------------------------------------------------------
# payload 생성
# ---------------------------------------------------------------------------


def resolve_max_tokens(row: dict[str, Any], cli_max_tokens: int) -> int:
    row_max_tokens = row.get("output_token_len", row.get("output_tokens", cli_max_tokens))
    if cli_max_tokens == DEFAULT_MAX_TOKENS:
        return int(row_max_tokens)
    return int(min(row_max_tokens, cli_max_tokens))


def messages_from_row(row: dict[str, Any]) -> list[dict[str, str]]:
    if row.get("messages") is not None:
        return row["messages"]
    if row.get("prompt") is not None:
        return [{"role": "user", "content": row["prompt"]}]
    raise ValueError("row에는 messages 또는 prompt 필드가 필요합니다.")


def build_payload(row: dict[str, Any], model: str, max_tokens: int,
                  workload_tag: str = "") -> dict[str, Any]:
    """OpenAI Chat Completions 요청 페이로드를 생성한다.

    vLLM 내부 eviction 계측을 위해 'user' 필드에 workload 태그를 포함한다.
    vLLM은 이 값을 ChatCompletionRequest.user로 파싱하며,
    계측 패치 적용 시 Request.workload_tag로 전파된다.
    """
    return {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        # 스트림 종료 시점에 usage 정보(캐시 hit 토큰 수 포함)를 수신한다.
        "stream_options": {"include_usage": True},
        "messages": messages_from_row(row),
        # vLLM 내부 block workload 태그 전달 — eviction attribution 계측용
        "user": workload_tag,
    }


# ---------------------------------------------------------------------------
# 단일 요청 전송
# ---------------------------------------------------------------------------


def extract_stream_text(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("delta") or {}).get("content") or ""


async def send_one(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    row: dict[str, Any],
    cli_max_tokens: int,
    semaphore: asyncio.Semaphore,
    workload_tag: str,
) -> dict[str, Any]:
    actual_max_tokens = resolve_max_tokens(row, cli_max_tokens)
    # workload_tag를 페이로드에 포함해 vLLM 내부 eviction 계측 경로로 전달한다.
    payload = build_payload(row, model, actual_max_tokens, workload_tag=workload_tag)

    async with semaphore:
        start_perf = time.perf_counter()
        start_wall = time.time()
        last_token_perf = start_perf
        ttft: float | None = None
        itls: list[float] = []
        prompt_tokens = None
        completion_tokens = None
        cached_tokens = None
        error: str | None = None

        try:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    error = f"http {response.status}: {(await response.text())[:200]}"
                else:
                    async for raw_line in response.content:
                        line = raw_line.decode("utf-8").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        usage = chunk.get("usage")
                        if usage:
                            prompt_tokens = usage.get("prompt_tokens")
                            completion_tokens = usage.get("completion_tokens")
                            cached_tokens = extract_cached_tokens(usage)
                        text = extract_stream_text(chunk)
                        if not text:
                            continue
                        now_perf = time.perf_counter()
                        if ttft is None:
                            ttft = now_perf - start_perf
                        else:
                            itls.append(now_perf - last_token_perf)
                        last_token_perf = now_perf
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        end_perf = time.perf_counter()
        end_wall = time.time()

    mean_itl = float(np.mean(itls)) if itls else None
    prompt_token_count = int(prompt_tokens) if isinstance(prompt_tokens, (int, float)) else None
    cached_token_count = int(cached_tokens) if isinstance(cached_tokens, (int, float)) else None

    # vLLM 버전에 따라 cached token 수가 prompt token 수보다 크게 보고될 수 있어 클램프한다.
    h_r = u_r = hit_rate = None
    if prompt_token_count is not None and cached_token_count is not None:
        h_r = max(0, min(cached_token_count, prompt_token_count))
        u_r = prompt_token_count - h_r
        hit_rate = h_r / prompt_token_count if prompt_token_count > 0 else 0.0

    return {
        "workload": workload_tag,
        "request_id": row.get("request_id"),
        "conversation_id": row.get("conversation_id"),
        "turn_id": row.get("turn_id"),
        "ttft": ttft,
        "itls": itls,
        "mean_itl": mean_itl,
        "tpot": mean_itl,
        "e2e": end_perf - start_perf,
        "start_wall": start_wall,
        "end_wall": end_wall,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "h_r": h_r,
        "l_r": prompt_token_count,
        "u_r": u_r,
        "hit_rate": hit_rate,
        "error": error,
    }


# ---------------------------------------------------------------------------
# 비동기 파이프라인
# ---------------------------------------------------------------------------


async def workload_producer(
    requests: list[dict[str, Any]],
    qps: float,
    workload_tag: str,
    queue: asyncio.Queue,
    seed: int,
) -> None:
    """독립적인 Poisson 도착 과정으로 요청을 큐에 투입한다."""
    rng = np.random.default_rng(seed)
    for request in requests:
        await queue.put({**request, "_workload": workload_tag})
        if qps > 0:
            await asyncio.sleep(rng.exponential(1.0 / qps))


async def consumer(
    queue: asyncio.Queue,
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    max_tokens: int,
    semaphore: asyncio.Semaphore,
    results: list[dict[str, Any]],
    progress_bar: tqdm,
) -> None:
    while True:
        row = await queue.get()
        if row is None:
            await queue.put(None)
            return
        workload_tag = row.pop("_workload", "unknown")
        result = await send_one(session, url, model, row, max_tokens, semaphore, workload_tag)
        results.append(result)
        status = "ERR" if result["error"] else "OK"
        ttft_text = f"{result['ttft'] * 1000:.0f}ms" if result["ttft"] else "-"
        progress_bar.set_postfix(workload=workload_tag, status=status, ttft=ttft_text)
        progress_bar.update(1)


# ---------------------------------------------------------------------------
# 결과 집계
# ---------------------------------------------------------------------------


def summarize_workload(
    rows: list[dict[str, Any]],
    tag: str,
    slo_ms: float,
    duration_seconds: float,
) -> None:
    ok = [r for r in rows if r["workload"] == tag and r["error"] is None and r["ttft"] is not None]
    failed = [r for r in rows if r["workload"] == tag and (r["error"] is not None or r["ttft"] is None)]

    if not ok:
        print(f"  [{tag}] 성공한 요청 없음 (실패: {len(failed)})")
        return

    ttfts = [r["ttft"] * 1000 for r in ok]
    tpots = [r["tpot"] * 1000 for r in ok if r["tpot"] is not None]
    hit_rates = [r["hit_rate"] for r in ok if r["hit_rate"] is not None]
    total_output = sum((r["completion_tokens"] or 0) for r in ok)
    slo_attainment = sum(1 for t in ttfts if t <= slo_ms) / (len(ok) + len(failed))

    print(f"\n  [{tag.upper()}]  성공: {len(ok)}  실패: {len(failed)}")
    print(f"  처리량: {len(ok) / duration_seconds:.2f} req/s  {total_output / duration_seconds:.1f} tok/s")
    print(f"  SLO attainment (TTFT ≤ {slo_ms:.0f}ms): {slo_attainment:.1%}")
    if hit_rates:
        print(f"  Cache hit rate: mean={np.mean(hit_rates):.3f}  p50={percentile(hit_rates, 50):.3f}")
    print(
        f"  TTFT(ms) mean={np.mean(ttfts):.1f} "
        f"p50={percentile(ttfts, 50):.1f} "
        f"p95={percentile(ttfts, 95):.1f} "
        f"p99={percentile(ttfts, 99):.1f}"
    )
    if tpots:
        print(
            f"  TPOT(ms) mean={np.mean(tpots):.1f} "
            f"p50={percentile(tpots, 50):.1f} "
            f"p95={percentile(tpots, 95):.1f} "
            f"p99={percentile(tpots, 99):.1f}"
        )


def print_summary(
    results: list[dict[str, Any]],
    duration_seconds: float,
    chat_slo_ms: float,
    rag_slo_ms: float,
) -> None:
    total_ok = sum(1 for r in results if r["error"] is None)
    total_failed = sum(1 for r in results if r["error"] is not None)
    print(f"\n{'=' * 50}")
    print(f"전체: {len(results)}  성공: {total_ok}  실패: {total_failed}  소요: {duration_seconds:.1f}s")
    summarize_workload(results, "chat", chat_slo_ms, duration_seconds)
    summarize_workload(results, "rag", rag_slo_ms, duration_seconds)


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    chat_trace = resolve_trace_path(
        args.chat_trace,
        "Chat",
        "  cd workloads/sharegpt && python build_sharegpt_workload.py "
        "--num-conversations 5000 --output sharegpt_conversation.jsonl",
    )
    rag_trace = resolve_trace_path(
        args.rag_trace,
        "RAG",
        "  cd workloads/squad && python build_squad_workload.py "
        "--num-requests 5000 --output squad_validation.jsonl",
    )
    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chat_requests = load_jsonl(chat_trace)
    rag_requests = load_jsonl(rag_trace)
    if args.num_chat_prompts > 0:
        chat_requests = chat_requests[: args.num_chat_prompts]
    if args.num_rag_prompts > 0:
        rag_requests = rag_requests[: args.num_rag_prompts]

    total = len(chat_requests) + len(rag_requests)
    print(
        f"chat={len(chat_requests)} @ {args.chat_qps} qps | "
        f"rag={len(rag_requests)} @ {args.rag_qps} qps | "
        f"total={total} | concurrency={args.max_concurrency} | url={args.url}"
    )
    print(f"chat_trace={chat_trace}")
    print(f"rag_trace={rag_trace}")
    print(f"output={output_path}")

    queue: asyncio.Queue = asyncio.Queue()
    semaphore = asyncio.Semaphore(args.max_concurrency)
    results: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=args.timeout_seconds)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        start_perf = time.perf_counter()
        with tqdm(total=total, unit="req", dynamic_ncols=True) as pbar:
            consumer_tasks = [
                asyncio.create_task(
                    consumer(
                        queue=queue,
                        session=session,
                        url=args.url,
                        model=args.model,
                        max_tokens=args.max_tokens,
                        semaphore=semaphore,
                        results=results,
                        progress_bar=pbar,
                    )
                )
                for _ in range(args.max_concurrency)
            ]

            # 두 workload의 Poisson 도착을 독립적으로 동시에 진행한다.
            await asyncio.gather(
                workload_producer(chat_requests, args.chat_qps, "chat", queue, seed=42),
                workload_producer(rag_requests, args.rag_qps, "rag", queue, seed=43),
            )
            # 두 producer가 모두 끝난 뒤 sentinel을 하나만 보낸다.
            await queue.put(None)
            await asyncio.gather(*consumer_tasks)

        duration_seconds = time.perf_counter() - start_perf

    write_jsonl(output_path, results)
    print_summary(results, duration_seconds, args.chat_slo_ms, args.rag_slo_ms)
    print(f"\n저장 완료: {output_path}")


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

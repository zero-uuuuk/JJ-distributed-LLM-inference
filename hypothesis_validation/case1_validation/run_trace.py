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
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_FALLBACK_MAX_TOKENS = 128
DEFAULT_MAX_TOKENS_BY_WORKLOAD = {
    "chat": 784,
    "rag": 205,
}


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


def write_json(path: Path, data: dict[str, Any]) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def resolve_summary_path(output_path: Path) -> Path:
    """요약 JSON은 원본 JSONL과 같은 디렉토리에 저장한다."""
    resolved_output = output_path.expanduser().resolve()
    return resolved_output.with_name(f"{resolved_output.stem}_summary.json")


# ---------------------------------------------------------------------------
# CLI 처리
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JSONL trace를 vLLM OpenAI 호환 서버로 전송합니다.",
    )
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api", choices=("chat", "completions"), default="chat")
    parser.add_argument("--url", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--qps", type=float, default=2.0)
    parser.add_argument("--max-concurrency", type=int, default=32)
    parser.add_argument("--num-prompts", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--workload-tag",
        default="default",
        help="결과 row에 붙을 workload 레이블 (예: chat, rag)",
    )
    parser.add_argument(
        "--slo-ms",
        type=float,
        default=None,
        help="TTFT SLO 기준값 (ms). 지정 시 SLO attainment를 출력합니다. (예: chat=400, rag=400)",
    )
    return parser.parse_args()


def resolve_default_url(api: str, url: str | None) -> str:
    if url is not None:
        return url
    if api == "chat":
        return "http://localhost:8000/v1/chat/completions"
    return "http://localhost:8000/v1/completions"


# ---------------------------------------------------------------------------
# 통계 유틸리티
# ---------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    return float(np.percentile(values, p)) if values else float("nan")


def mean(values: list[float]) -> float:
    return float(np.mean(values)) if values else float("nan")


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


def resolve_max_token_cap(workload_tag: str) -> int | None:
    """workload별 trace 최대 output length를 기본 cap으로 사용한다."""
    return DEFAULT_MAX_TOKENS_BY_WORKLOAD.get(workload_tag.lower())


def resolve_max_tokens(row: dict[str, Any], workload_tag: str) -> int:
    """요청별 output_token_len을 보존하되 workload별 최대값으로 truncation을 방지한다."""
    row_max_tokens = row.get("output_token_len", row.get("output_tokens"))
    token_cap = resolve_max_token_cap(workload_tag)

    if row_max_tokens is None and token_cap is None:
        return DEFAULT_FALLBACK_MAX_TOKENS
    if row_max_tokens is None:
        return int(token_cap)
    if token_cap is None:
        return int(row_max_tokens)
    return int(min(row_max_tokens, token_cap))


def messages_from_row(row: dict[str, Any]) -> list[dict[str, str]]:
    if row.get("messages") is not None:
        return row["messages"]
    if row.get("prompt") is not None:
        return [{"role": "user", "content": row["prompt"]}]
    raise ValueError("row에는 messages 또는 prompt 필드가 필요합니다.")


def prompt_from_row(row: dict[str, Any]) -> str:
    if row.get("prompt") is not None:
        return row["prompt"]
    if row.get("messages") is None:
        raise ValueError("row에는 messages 또는 prompt 필드가 필요합니다.")
    parts: list[str] = []
    for msg in row["messages"]:
        prefix = "User: " if msg["role"] == "user" else "Assistant: "
        parts.append(prefix + msg["content"])
    parts.append("Assistant:")
    return "\n".join(parts)


def build_payload(
    row: dict[str, Any], model: str, max_tokens: int, api: str, workload_tag: str = ""
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "user": workload_tag,  # vLLM 내부 block workload 태그 전달 — eviction attribution 계측용
    }
    if api == "chat":
        payload["messages"] = messages_from_row(row)
    else:
        payload["prompt"] = prompt_from_row(row)
    return payload


# ---------------------------------------------------------------------------
# 단일 요청 전송
# ---------------------------------------------------------------------------


def extract_stream_text(chunk: dict[str, Any], api: str) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    first = choices[0]
    if api == "chat":
        return (first.get("delta") or {}).get("content") or ""
    return first.get("text", "")


async def send_one(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    row: dict[str, Any],
    semaphore: asyncio.Semaphore,
    api: str,
    workload_tag: str,
) -> dict[str, Any]:
    actual_max_tokens = resolve_max_tokens(row, workload_tag)
    payload = build_payload(row, model, actual_max_tokens, api, workload_tag)

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
                        text = extract_stream_text(chunk, api)
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


async def producer(requests: list[dict[str, Any]], qps: float, queue: asyncio.Queue) -> None:
    rng = np.random.default_rng(42)
    for request in requests:
        await queue.put(request)
        if qps > 0:
            await asyncio.sleep(rng.exponential(1.0 / qps))
    await queue.put(None)


async def consumer(
    queue: asyncio.Queue,
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    semaphore: asyncio.Semaphore,
    results: list[dict[str, Any]],
    api: str,
    workload_tag: str,
    progress_bar: tqdm,
) -> None:
    while True:
        row = await queue.get()
        if row is None:
            await queue.put(None)
            return
        result = await send_one(session, url, model, row, semaphore, api, workload_tag)
        results.append(result)
        status = "ERR" if result["error"] else "OK"
        ttft_text = f"{result['ttft'] * 1000:.0f}ms" if result["ttft"] else "-"
        progress_bar.set_postfix(status=status, ttft=ttft_text)
        progress_bar.update(1)


# ---------------------------------------------------------------------------
# 결과 집계
# ---------------------------------------------------------------------------


def build_summary(
    results: list[dict[str, Any]],
    duration_seconds: float,
    slo_ms: float | None,
    workload_tag: str,
) -> dict[str, Any]:
    """콘솔 출력과 파일 저장에 함께 쓰는 집계 지표를 생성한다."""
    ok = [r for r in results if r["error"] is None and r["ttft"] is not None]
    failed = [r for r in results if r["error"] is not None or r["ttft"] is None]

    ttfts = [r["ttft"] * 1000 for r in ok]
    e2es = [r["e2e"] * 1000 for r in ok]
    tpots = [r["tpot"] * 1000 for r in ok if r["tpot"] is not None]
    flat_itls = [itl * 1000 for r in ok for itl in r["itls"]]
    total_input = sum((r["prompt_tokens"] or 0) for r in ok)
    total_output = sum((r["completion_tokens"] or 0) for r in ok)
    hit_rates = [r["hit_rate"] for r in ok if r["hit_rate"] is not None]
    slo_attainment = (
        sum(1 for t in ttfts if t <= slo_ms) / len(results)
        if slo_ms is not None and results
        else None
    )

    return {
        "workload": workload_tag,
        "duration_seconds": duration_seconds,
        "n_total": len(results),
        "n_ok": len(ok),
        "n_failed": len(failed),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "throughput_req_per_s": len(ok) / duration_seconds if duration_seconds > 0 else 0.0,
        "throughput_tok_per_s": total_output / duration_seconds if duration_seconds > 0 else 0.0,
        "slo_ms": slo_ms,
        "slo_attainment": slo_attainment,
        "hit_rate_mean": mean(hit_rates),
        "hit_rate_p50": percentile(hit_rates, 50),
        "ttft_ms_mean": mean(ttfts),
        "ttft_ms_p50": percentile(ttfts, 50),
        "ttft_ms_p95": percentile(ttfts, 95),
        "ttft_ms_p99": percentile(ttfts, 99),
        "tpot_ms_mean": mean(tpots),
        "tpot_ms_p50": percentile(tpots, 50),
        "tpot_ms_p95": percentile(tpots, 95),
        "tpot_ms_p99": percentile(tpots, 99),
        "itl_ms_mean": mean(flat_itls),
        "itl_ms_p50": percentile(flat_itls, 50),
        "itl_ms_p95": percentile(flat_itls, 95),
        "itl_ms_p99": percentile(flat_itls, 99),
        "e2e_ms_mean": mean(e2es),
        "e2e_ms_p50": percentile(e2es, 50),
        "e2e_ms_p95": percentile(e2es, 95),
        "e2e_ms_p99": percentile(e2es, 99),
        "failed_examples": [
            {"request_id": r.get("request_id"), "error": r.get("error")}
            for r in failed[:3]
        ],
    }


def print_summary(summary: dict[str, Any]) -> None:
    """집계 지표를 콘솔에 출력한다."""
    print(f"\n{'=' * 50}")
    print(
        f"전체: {summary['n_total']}  성공: {summary['n_ok']}  "
        f"실패: {summary['n_failed']}  소요: {summary['duration_seconds']:.1f}s"
    )

    if summary["n_ok"] == 0:
        print(f"  [{summary['workload'].upper()}] 성공한 요청 없음 (실패: {summary['n_failed']})")
        return

    print(f"\n  [{summary['workload'].upper()}]  성공: {summary['n_ok']}  실패: {summary['n_failed']}")
    print(f"  입력 토큰: {summary['total_input_tokens']}  출력 토큰: {summary['total_output_tokens']}")
    print(f"  처리량: {summary['throughput_req_per_s']:.2f} req/s  {summary['throughput_tok_per_s']:.1f} tok/s")
    if summary["slo_attainment"] is not None:
        print(f"  SLO attainment (TTFT ≤ {summary['slo_ms']:.0f}ms): {summary['slo_attainment']:.1%}")
    if summary["hit_rate_mean"] == summary["hit_rate_mean"]:
        print(f"  Cache hit rate: mean={summary['hit_rate_mean']:.3f}  p50={summary['hit_rate_p50']:.3f}")
    print(
        f"  TTFT(ms) mean={summary['ttft_ms_mean']:.1f} "
        f"p50={summary['ttft_ms_p50']:.1f} "
        f"p95={summary['ttft_ms_p95']:.1f} "
        f"p99={summary['ttft_ms_p99']:.1f}"
    )
    if summary["tpot_ms_mean"] == summary["tpot_ms_mean"]:
        print(
            f"  TPOT(ms) mean={summary['tpot_ms_mean']:.1f} "
            f"p50={summary['tpot_ms_p50']:.1f} "
            f"p95={summary['tpot_ms_p95']:.1f} "
            f"p99={summary['tpot_ms_p99']:.1f}"
        )
    if summary["itl_ms_mean"] == summary["itl_ms_mean"]:
        print(
            f"  ITL(ms)  mean={summary['itl_ms_mean']:.1f} "
            f"p50={summary['itl_ms_p50']:.1f} "
            f"p95={summary['itl_ms_p95']:.1f} "
            f"p99={summary['itl_ms_p99']:.1f}"
        )
    print(
        f"  E2E(ms)  mean={summary['e2e_ms_mean']:.1f} "
        f"p50={summary['e2e_ms_p50']:.1f} "
        f"p95={summary['e2e_ms_p95']:.1f} "
        f"p99={summary['e2e_ms_p99']:.1f}"
    )
    if summary["failed_examples"]:
        print("\n  첫 실패 3개:")
        for r in summary["failed_examples"]:
            print(f"    {r['request_id']} | {r['error']}")


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    requests = load_jsonl(args.trace)
    if args.num_prompts > 0:
        requests = requests[: args.num_prompts]

    url = resolve_default_url(args.api, args.url)
    print(
        f"loaded {len(requests)} requests | qps={args.qps} "
        f"| concurrency={args.max_concurrency} | api={args.api} | url={url} "
        f"| workload={args.workload_tag}"
    )

    queue: asyncio.Queue = asyncio.Queue()
    semaphore = asyncio.Semaphore(args.max_concurrency)
    results: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=args.timeout_seconds)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        start_perf = time.perf_counter()
        with tqdm(total=len(requests), unit="req", dynamic_ncols=True) as pbar:
            producer_task = asyncio.create_task(producer(requests, args.qps, queue))
            consumer_tasks = [
                asyncio.create_task(
                    consumer(
                        queue=queue,
                        session=session,
                        url=url,
                        model=args.model,
                        semaphore=semaphore,
                        results=results,
                        api=args.api,
                        workload_tag=args.workload_tag,
                        progress_bar=pbar,
                    )
                )
                for _ in range(args.max_concurrency)
            ]
            await producer_task
            await asyncio.gather(*consumer_tasks)
        duration_seconds = time.perf_counter() - start_perf

    summary = build_summary(results, duration_seconds, args.slo_ms, args.workload_tag)
    summary_path = resolve_summary_path(args.output)

    write_jsonl(args.output, results)
    write_json(summary_path, summary)
    print_summary(summary)
    print(f"\n저장 완료: {args.output}")
    print(f"요약 저장 완료: {summary_path}")


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

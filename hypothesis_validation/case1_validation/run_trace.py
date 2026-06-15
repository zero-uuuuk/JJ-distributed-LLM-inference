"""역할: 단일 JSONL trace를 vLLM 서버로 전송해 workload별 baseline 지표를 측정한다.

상세 과정:
  1. 입력 trace를 읽고 지정한 개수만큼 요청을 선택한다.
  2. Poisson 또는 Agent session scheduler로 요청 도착 시간을 만든다.
  3. vLLM OpenAI 호환 API에 streaming 요청을 보내 TTFT·TPOT·cache hit rate를 기록한다.
  4. workload 태그별 output token cap과 SLO 기준으로 요약 지표를 저장한다.
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
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_FALLBACK_MAX_TOKENS = 128
DEFAULT_CHAT_URL = "http://localhost:8000/v1/chat/completions"
DEFAULT_COMPLETIONS_URL = "http://localhost:8000/v1/completions"
DEFAULT_MAX_TOKENS_BY_WORKLOAD = {
    "chat": 691,
    "rag": 205,
    "longctx": 41,
}


# ---------------------------------------------------------------------------
# JSONL 입출력
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """JSONL trace를 dict row 목록으로 로드한다."""
    resolved_path = path.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    with resolved_path.open("r", encoding="utf-8") as input_file:
        for line in input_file:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """실험 결과 row를 JSONL로 저장한다."""
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    """요약 지표를 JSON으로 저장한다."""
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as output_file:
        json.dump(data, output_file, ensure_ascii=False, indent=2)


def resolve_summary_path(output_path: Path) -> Path:
    """요약 JSON은 원본 JSONL과 같은 디렉토리에 저장한다."""
    resolved_output = output_path.expanduser().resolve()
    return resolved_output.with_name(f"{resolved_output.stem}_summary.json")


def resolve_trace_path(path: Path, label: str, build_hint: str) -> Path:
    """trace 경로를 절대 경로로 변환하고, 없으면 생성 방법을 안내한다."""
    resolved_path = path.expanduser().resolve()
    if resolved_path.is_file():
        return resolved_path
    raise SystemExit(
        f"{label} trace를 찾을 수 없습니다: {resolved_path}\n"
        f"먼저 워크로드를 생성하세요:\n{build_hint}"
    )


# ---------------------------------------------------------------------------
# CLI 처리
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JSONL trace를 vLLM OpenAI 호환 서버로 전송합니다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
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
        help="결과 row에 붙을 workload 레이블입니다. 예: chat, rag, longctx, agent",
    )
    parser.add_argument(
        "--slo-ms",
        type=float,
        default=None,
        help="TTFT SLO 기준값(ms). 지정 시 SLO attainment를 출력합니다.",
    )
    parser.add_argument(
        "--scheduler",
        choices=("poisson", "agent-session"),
        default="poisson",
        help="단일 trace 요청 도착 모델입니다.",
    )
    parser.add_argument("--agent-target-rps", type=float, default=5.0)
    parser.add_argument("--agent-steps-per-session", type=float, default=10.0)
    parser.add_argument(
        "--agent-tool-gap-mode",
        choices=("fixed", "gaussian", "exponential"),
        default="fixed",
    )
    parser.add_argument("--agent-tool-gap-seconds", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-mean", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-std", type=float, default=2.0)
    parser.add_argument("--agent-tool-gap-min", type=float, default=0.0)
    parser.add_argument("--agent-tool-gap-max", type=float, default=20.0)
    return parser.parse_args()


def resolve_default_url(api: str, url: str | None) -> str:
    """명시 URL이 없으면 API 종류에 맞는 기본 vLLM endpoint를 반환한다."""
    if url is not None:
        return url
    if api == "chat":
        return DEFAULT_CHAT_URL
    return DEFAULT_COMPLETIONS_URL


# ---------------------------------------------------------------------------
# 통계 유틸리티
# ---------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    return float(np.percentile(values, p)) if values else float("nan")


def mean(values: list[float]) -> float:
    return float(np.mean(values)) if values else float("nan")


def extract_cached_tokens(usage: dict[str, Any] | None) -> int | None:
    """vLLM/OpenAI 호환 usage 객체에서 cached token 수를 최대한 호환적으로 추출한다."""
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
    """요청별 output_token_len을 보존하되 workload별 최대값으로 clamp한다."""
    row_max_tokens = row.get("output_token_len", row.get("output_tokens"))
    token_cap = resolve_max_token_cap(workload_tag)

    if row_max_tokens is None and token_cap is None:
        return DEFAULT_FALLBACK_MAX_TOKENS
    if row_max_tokens is None:
        return max(1, int(token_cap))
    if token_cap is None:
        return max(1, int(row_max_tokens))
    return max(1, int(min(row_max_tokens, token_cap)))


def messages_from_row(row: dict[str, Any]) -> list[dict[str, str]]:
    """trace row에서 Chat Completions messages를 만든다."""
    if row.get("messages") is not None:
        return row["messages"]
    if row.get("prompt") is not None:
        return [{"role": "user", "content": row["prompt"]}]
    raise ValueError("row에는 messages 또는 prompt 필드가 필요합니다.")


def prompt_from_row(row: dict[str, Any]) -> str:
    """trace row에서 legacy Completions prompt를 만든다."""
    if row.get("prompt") is not None:
        return row["prompt"]
    if row.get("messages") is None:
        raise ValueError("row에는 messages 또는 prompt 필드가 필요합니다.")

    parts: list[str] = []
    for message in row["messages"]:
        role = message.get("role", "user")
        prefix = "Assistant: " if role == "assistant" else "User: "
        parts.append(prefix + str(message.get("content", "")))
    parts.append("Assistant:")
    return "\n".join(parts)


def build_payload(
    row: dict[str, Any],
    model: str,
    max_tokens: int,
    api: str,
    workload_tag: str,
) -> dict[str, Any]:
    """OpenAI 호환 요청 payload를 구성하고 workload tag를 user 필드로 전달한다."""
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "user": workload_tag,
    }
    if api == "chat":
        payload["messages"] = messages_from_row(row)
    else:
        payload["prompt"] = prompt_from_row(row)
    return payload


def extract_stream_text(chunk: dict[str, Any], api: str) -> str:
    """stream chunk에서 새로 생성된 텍스트 조각을 추출한다."""
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    first = choices[0]
    if api == "chat":
        return (first.get("delta") or {}).get("content") or ""
    return first.get("text", "")


# ---------------------------------------------------------------------------
# 단일 요청 전송
# ---------------------------------------------------------------------------


async def send_one(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    row: dict[str, Any],
    semaphore: asyncio.Semaphore,
    api: str,
    workload_tag: str,
) -> dict[str, Any]:
    """단일 요청을 streaming으로 전송하고 latency/cache 지표를 반환한다."""
    actual_max_tokens = resolve_max_tokens(row, workload_tag)
    payload = build_payload(row, model, actual_max_tokens, api, workload_tag)
    scheduled_perf = row.get("_scheduled_perf")
    scheduled_wall = row.get("_scheduled_wall")

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

    # 일부 vLLM 버전은 cached token 수를 prompt token보다 크게 보고해 안전하게 clamp한다.
    h_r = u_r = hit_rate = None
    if prompt_token_count is not None and cached_token_count is not None:
        h_r = max(0, min(cached_token_count, prompt_token_count))
        u_r = prompt_token_count - h_r
        hit_rate = h_r / prompt_token_count if prompt_token_count > 0 else 0.0

    queue_delay = None
    if isinstance(scheduled_perf, (int, float)):
        queue_delay = start_perf - float(scheduled_perf)

    return {
        "workload": workload_tag,
        "request_id": row.get("request_id"),
        "conversation_id": row.get("conversation_id"),
        "turn_id": row.get("turn_id"),
        "session_id": row.get("session_id"),
        "task_id": row.get("task_id"),
        "step_id": row.get("step_id"),
        "session_step_count": row.get("session_step_count"),
        "tool_gap_seconds": row.get("_tool_gap_seconds", row.get("tool_gap_seconds")),
        "scheduled_wall": scheduled_wall,
        "queue_delay": queue_delay,
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
# 스케줄러
# ---------------------------------------------------------------------------


async def record_result(
    result: dict[str, Any],
    results: list[dict[str, Any]],
    progress_bar: tqdm,
    progress_lock: asyncio.Lock,
) -> None:
    """동시 요청 결과를 안전하게 모으고 progress bar를 갱신한다."""
    async with progress_lock:
        results.append(result)
        status = "ERR" if result["error"] else "OK"
        ttft_text = f"{result['ttft'] * 1000:.0f}ms" if result["ttft"] else "-"
        progress_bar.set_postfix(workload=result["workload"], status=status, ttft=ttft_text)
        progress_bar.update(1)


async def run_poisson_workload(
    requests: list[dict[str, Any]],
    qps: float,
    workload_tag: str,
    seed: int,
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    semaphore: asyncio.Semaphore,
    results: list[dict[str, Any]],
    progress_bar: tqdm,
    progress_lock: asyncio.Lock,
    api: str = "chat",
) -> None:
    """독립 Poisson 도착 과정으로 요청을 전송한다."""
    rng = np.random.default_rng(seed)
    tasks: list[asyncio.Task[None]] = []

    async def send_and_record(row: dict[str, Any]) -> None:
        result = await send_one(session, url, model, row, semaphore, api, workload_tag)
        await record_result(result, results, progress_bar, progress_lock)

    for request in requests:
        row = {
            **request,
            "_workload": workload_tag,
            "_scheduled_perf": time.perf_counter(),
            "_scheduled_wall": time.time(),
        }
        tasks.append(asyncio.create_task(send_and_record(row)))
        if qps > 0:
            await asyncio.sleep(float(rng.exponential(1.0 / qps)))

    if tasks:
        await asyncio.gather(*tasks)


def group_agent_sessions(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Agent trace row를 session 단위로 묶고 step 순서로 정렬한다."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    fallback_index = 0
    for row in rows:
        session_id = row.get("session_id") or row.get("conversation_id")
        if session_id is None:
            session_id = f"session_{fallback_index:06d}"
            fallback_index += 1
        grouped.setdefault(str(session_id), []).append(row)

    sessions = list(grouped.values())
    for session_rows in sessions:
        session_rows.sort(key=lambda item: int(item.get("step_id", item.get("turn_id", 0)) or 0))
    return sessions


def session_start_delays(num_sessions: int, session_start_rps: float, seed: int) -> list[float]:
    """Agent session 시작 시각을 Poisson arrival로 샘플링한다."""
    delays: list[float] = []
    elapsed = 0.0
    rng = np.random.default_rng(seed)
    for index in range(num_sessions):
        if index > 0 and session_start_rps > 0:
            elapsed += float(rng.exponential(1.0 / session_start_rps))
        delays.append(elapsed)
    return delays


def sample_tool_gap(
    rng: np.random.Generator,
    mode: str,
    fixed_seconds: float,
    mean_seconds: float,
    std_seconds: float,
    min_seconds: float,
    max_seconds: float,
) -> float:
    """Agent step 사이 tool execution gap을 샘플링한다."""
    if mode == "fixed":
        return max(0.0, float(fixed_seconds))
    if mode == "exponential":
        sampled = float(rng.exponential(mean_seconds))
        return min(sampled, float(max_seconds))
    sampled = float(rng.normal(mean_seconds, std_seconds))
    return max(float(min_seconds), min(sampled, float(max_seconds)))


async def run_agent_session(
    session_rows: list[dict[str, Any]],
    start_delay: float,
    session_index: int,
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    semaphore: asyncio.Semaphore,
    results: list[dict[str, Any]],
    progress_bar: tqdm,
    progress_lock: asyncio.Lock,
    gap_mode: str,
    fixed_gap_seconds: float,
    gap_mean: float,
    gap_std: float,
    gap_min: float,
    gap_max: float,
    api: str = "chat",
) -> None:
    """하나의 Agent session을 step 완료 이후 tool gap 구조로 실행한다."""
    if start_delay > 0:
        await asyncio.sleep(start_delay)

    rng = np.random.default_rng(10_000 + session_index)
    previous_gap: float | None = None

    for index, request in enumerate(session_rows):
        row = {
            **request,
            "_workload": "agent",
            "_tool_gap_seconds": previous_gap,
            "_scheduled_perf": time.perf_counter(),
            "_scheduled_wall": time.time(),
        }
        result = await send_one(session, url, model, row, semaphore, api, "agent")
        await record_result(result, results, progress_bar, progress_lock)

        if index < len(session_rows) - 1:
            previous_gap = sample_tool_gap(
                rng=rng,
                mode=gap_mode,
                fixed_seconds=fixed_gap_seconds,
                mean_seconds=gap_mean,
                std_seconds=gap_std,
                min_seconds=gap_min,
                max_seconds=gap_max,
            )
            await asyncio.sleep(previous_gap)


async def run_agent_workload(
    agent_requests: list[dict[str, Any]],
    agent_target_rps: float,
    agent_steps_per_session: float,
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    semaphore: asyncio.Semaphore,
    results: list[dict[str, Any]],
    progress_bar: tqdm,
    progress_lock: asyncio.Lock,
    gap_mode: str,
    fixed_gap_seconds: float,
    gap_mean: float,
    gap_std: float,
    gap_min: float,
    gap_max: float,
    api: str = "chat",
) -> None:
    """Agent workload 전체를 session start Poisson + session 내부 순차 step으로 실행한다."""
    sessions = group_agent_sessions(agent_requests)
    session_start_rps = (
        agent_target_rps / agent_steps_per_session
        if agent_target_rps > 0 and agent_steps_per_session > 0
        else 0.0
    )
    delays = session_start_delays(len(sessions), session_start_rps, seed=42)
    tasks = [
        asyncio.create_task(
            run_agent_session(
                session_rows=session_rows,
                start_delay=delays[index],
                session_index=index,
                session=session,
                url=url,
                model=model,
                semaphore=semaphore,
                results=results,
                progress_bar=progress_bar,
                progress_lock=progress_lock,
                gap_mode=gap_mode,
                fixed_gap_seconds=fixed_gap_seconds,
                gap_mean=gap_mean,
                gap_std=gap_std,
                gap_min=gap_min,
                gap_max=gap_max,
                api=api,
            )
        )
        for index, session_rows in enumerate(sessions)
    ]
    if tasks:
        await asyncio.gather(*tasks)


def build_agent_scheduler_summary(
    target_rps: float,
    steps_per_session: float,
    gap_mode: str,
    gap_seconds: float,
    gap_mean: float,
    gap_std: float,
    gap_min: float,
    gap_max: float,
) -> dict[str, Any]:
    """Agent scheduler 설정을 summary JSON에 남길 형태로 구성한다."""
    return {
        "scheduler": "agent-session",
        "agent_target_rps": target_rps,
        "agent_steps_per_session": steps_per_session,
        "agent_session_start_rps": (
            target_rps / steps_per_session
            if target_rps > 0 and steps_per_session > 0
            else 0.0
        ),
        "agent_tool_gap_mode": gap_mode,
        "agent_tool_gap_seconds": gap_seconds,
        "agent_tool_gap_mean": gap_mean,
        "agent_tool_gap_std": gap_std,
        "agent_tool_gap_min": gap_min,
        "agent_tool_gap_max": gap_max,
    }


# ---------------------------------------------------------------------------
# 결과 집계
# ---------------------------------------------------------------------------


def summarize_workload(
    rows: list[dict[str, Any]],
    tag: str,
    slo_ms: float | None,
    duration_seconds: float,
) -> dict[str, Any]:
    """workload 하나의 latency/cache/throughput 지표를 집계한다."""
    ok = [r for r in rows if r["workload"] == tag and r["error"] is None and r["ttft"] is not None]
    failed = [r for r in rows if r["workload"] == tag and (r["error"] is not None or r["ttft"] is None)]

    ttfts = [r["ttft"] * 1000 for r in ok]
    e2es = [r["e2e"] * 1000 for r in ok]
    tpots = [r["tpot"] * 1000 for r in ok if r["tpot"] is not None]
    flat_itls = [itl * 1000 for r in ok for itl in r["itls"]]
    hit_rates = [r["hit_rate"] for r in ok if r["hit_rate"] is not None]
    queue_delays = [
        r["queue_delay"] * 1000
        for r in ok
        if isinstance(r.get("queue_delay"), (int, float))
    ]
    total_input = sum((r["prompt_tokens"] or 0) for r in ok)
    total_output = sum((r["completion_tokens"] or 0) for r in ok)
    slo_attainment = (
        sum(1 for ttft in ttfts if ttft <= slo_ms) / (len(ok) + len(failed))
        if slo_ms is not None and (ok or failed)
        else None
    )

    return {
        "workload": tag,
        "n_total": len(ok) + len(failed),
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
        "queue_delay_ms_mean": mean(queue_delays),
        "queue_delay_ms_p95": percentile(queue_delays, 95),
        "failed_examples": [
            {"request_id": r.get("request_id"), "error": r.get("error")}
            for r in failed[:3]
        ],
    }


def build_single_summary(
    results: list[dict[str, Any]],
    duration_seconds: float,
    slo_ms: float | None,
    workload_tag: str,
    scheduler: dict[str, Any],
) -> dict[str, Any]:
    """단일 trace summary는 기존 top-level schema를 유지하고 scheduler만 추가한다."""
    summary = summarize_workload(results, workload_tag, slo_ms, duration_seconds)
    summary["duration_seconds"] = duration_seconds
    summary["scheduler"] = scheduler
    return summary


def build_mixed_summary(
    results: list[dict[str, Any]],
    duration_seconds: float,
    workload_slos: dict[str, float | None],
    scheduler: dict[str, Any],
    antagonist_tag: str | None = None,
) -> dict[str, Any]:
    """mixed workload summary를 workload 태그별로 집계한다."""
    total_ok = sum(1 for row in results if row["error"] is None and row["ttft"] is not None)
    total_failed = len(results) - total_ok
    workload_tags = list(workload_slos)
    summary: dict[str, Any] = {
        "duration_seconds": duration_seconds,
        "n_total": len(results),
        "n_ok": total_ok,
        "n_failed": total_failed,
        "scheduler": scheduler,
        "workloads": workload_tags,
    }
    if antagonist_tag is not None:
        summary["antagonist_workload"] = antagonist_tag
    for tag in workload_tags:
        summary[tag] = summarize_workload(results, tag, workload_slos[tag], duration_seconds)
    return summary


def print_workload_summary(workload_summary: dict[str, Any]) -> None:
    """workload 하나의 요약 지표를 콘솔에 출력한다."""
    tag = workload_summary["workload"]
    if workload_summary["n_ok"] == 0:
        print(f"\n  [{tag.upper()}] 성공한 요청 없음 (실패: {workload_summary['n_failed']})")
        return

    print(
        f"\n  [{tag.upper()}]  성공: {workload_summary['n_ok']}  "
        f"실패: {workload_summary['n_failed']}"
    )
    print(
        f"  입력 토큰: {workload_summary['total_input_tokens']}  "
        f"출력 토큰: {workload_summary['total_output_tokens']}"
    )
    print(
        f"  처리량: {workload_summary['throughput_req_per_s']:.2f} req/s  "
        f"{workload_summary['throughput_tok_per_s']:.1f} tok/s"
    )
    if workload_summary["slo_attainment"] is not None:
        print(
            f"  SLO attainment (TTFT <= {workload_summary['slo_ms']:.0f}ms): "
            f"{workload_summary['slo_attainment']:.1%}"
        )
    if workload_summary["hit_rate_mean"] == workload_summary["hit_rate_mean"]:
        print(
            f"  Cache hit rate: mean={workload_summary['hit_rate_mean']:.3f}  "
            f"p50={workload_summary['hit_rate_p50']:.3f}"
        )
    print(
        f"  TTFT(ms) mean={workload_summary['ttft_ms_mean']:.1f} "
        f"p50={workload_summary['ttft_ms_p50']:.1f} "
        f"p95={workload_summary['ttft_ms_p95']:.1f} "
        f"p99={workload_summary['ttft_ms_p99']:.1f}"
    )
    if workload_summary["tpot_ms_mean"] == workload_summary["tpot_ms_mean"]:
        print(
            f"  TPOT(ms) mean={workload_summary['tpot_ms_mean']:.1f} "
            f"p50={workload_summary['tpot_ms_p50']:.1f} "
            f"p95={workload_summary['tpot_ms_p95']:.1f} "
            f"p99={workload_summary['tpot_ms_p99']:.1f}"
        )
    if workload_summary["queue_delay_ms_mean"] == workload_summary["queue_delay_ms_mean"]:
        print(
            f"  Queue delay(ms) mean={workload_summary['queue_delay_ms_mean']:.1f} "
            f"p95={workload_summary['queue_delay_ms_p95']:.1f}"
        )
    if workload_summary["failed_examples"]:
        print("\n  첫 실패 3개:")
        for row in workload_summary["failed_examples"]:
            print(f"    {row['request_id']} | {row['error']}")


def print_single_summary(summary: dict[str, Any]) -> None:
    """단일 trace summary를 콘솔에 출력한다."""
    print(f"\n{'=' * 50}")
    print(
        f"전체: {summary['n_total']}  성공: {summary['n_ok']}  "
        f"실패: {summary['n_failed']}  소요: {summary['duration_seconds']:.1f}s"
    )
    print_workload_summary(summary)


def print_mixed_summary(summary: dict[str, Any], workload_tags: list[str]) -> None:
    """mixed summary를 workload 순서대로 콘솔에 출력한다."""
    print(f"\n{'=' * 50}")
    print(
        f"전체: {summary['n_total']}  성공: {summary['n_ok']}  "
        f"실패: {summary['n_failed']}  소요: {summary['duration_seconds']:.1f}s"
    )
    for tag in workload_tags:
        print_workload_summary(summary[tag])


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    trace_path = resolve_trace_path(
        args.trace,
        "Input",
        "  python workloads/<workload>/build_<workload>_workload.py --output <trace>.jsonl",
    )
    requests = load_jsonl(trace_path)
    if args.num_prompts > 0:
        requests = requests[: args.num_prompts]

    url = resolve_default_url(args.api, args.url)
    scheduler = {"scheduler": args.scheduler, "qps": args.qps}
    if args.scheduler == "agent-session":
        scheduler = build_agent_scheduler_summary(
            target_rps=args.agent_target_rps,
            steps_per_session=args.agent_steps_per_session,
            gap_mode=args.agent_tool_gap_mode,
            gap_seconds=args.agent_tool_gap_seconds,
            gap_mean=args.agent_tool_gap_mean,
            gap_std=args.agent_tool_gap_std,
            gap_min=args.agent_tool_gap_min,
            gap_max=args.agent_tool_gap_max,
        )

    print(
        f"loaded {len(requests)} requests | scheduler={args.scheduler} "
        f"| concurrency={args.max_concurrency} | api={args.api} | url={url} "
        f"| workload={args.workload_tag}"
    )
    print(f"trace={trace_path}")

    semaphore = asyncio.Semaphore(args.max_concurrency)
    timeout = aiohttp.ClientTimeout(total=args.timeout_seconds)
    results: list[dict[str, Any]] = []
    progress_lock = asyncio.Lock()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        start_perf = time.perf_counter()
        with tqdm(total=len(requests), unit="req", dynamic_ncols=True) as progress_bar:
            if args.scheduler == "agent-session":
                await run_agent_workload(
                    agent_requests=requests,
                    agent_target_rps=args.agent_target_rps,
                    agent_steps_per_session=args.agent_steps_per_session,
                    session=session,
                    url=url,
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
                    api=args.api,
                )
            else:
                await run_poisson_workload(
                    requests=requests,
                    qps=args.qps,
                    workload_tag=args.workload_tag,
                    seed=42,
                    session=session,
                    url=url,
                    model=args.model,
                    semaphore=semaphore,
                    results=results,
                    progress_bar=progress_bar,
                    progress_lock=progress_lock,
                    api=args.api,
                )
        duration_seconds = time.perf_counter() - start_perf

    workload_tag = "agent" if args.scheduler == "agent-session" else args.workload_tag
    summary = build_single_summary(
        results=results,
        duration_seconds=duration_seconds,
        slo_ms=args.slo_ms,
        workload_tag=workload_tag,
        scheduler=scheduler,
    )
    summary_path = resolve_summary_path(args.output)

    write_jsonl(args.output, results)
    write_json(summary_path, summary)
    print_single_summary(summary)
    print(f"\n저장 완료: {args.output}")
    print(f"요약 저장 완료: {summary_path}")


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

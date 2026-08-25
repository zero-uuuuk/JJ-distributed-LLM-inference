"""tokenized workload를 Poisson arrival로 vLLM에 전송하고 cache 지표를 저장한다."""

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import aiohttp
from tqdm import tqdm

# ---------------------------------------------------------------------------
# 요청 전송
# ---------------------------------------------------------------------------


async def send_request(
    session: aiohttp.ClientSession,
    record: dict[str, Any],
    scheduled_at: float,
    run_started: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    """단일 streaming 요청을 전송하고 latency/cache 지표를 반환한다."""
    # 요청 실행 기준시각 및 도착시각 대기
    loop = asyncio.get_running_loop()
    await asyncio.sleep(max(0.0, run_started + scheduled_at - loop.time()))
    sent_at = loop.time() - run_started

    # 응답 usage·latency 측정값 초기화
    vllm_prompt_tokens = cached_tokens = completion_tokens = ttft_ms = error = None

    # OpenAI 호환 vLLM 요청 payload 구성
    payload = {
        "model": config["vllm"]["model"],
        "messages": record["messages"],
        "max_tokens": config["vllm"]["max_tokens"],
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "user": record["workload"],
    }

    try:
        # streaming HTTP 요청 전송
        async with session.post(config["vllm"]["url"], json=payload) as response:
            if response.status != 200:
                # HTTP 오류 응답 저장
                error = f"http {response.status}: {(await response.text())[:200]}"
            else:
                # SSE 응답 chunk 순회
                async for raw_line in response.content:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    # JSON chunk 변환
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    # 첫 응답 chunk 기준 TTFT 계산
                    if ttft_ms is None:
                        ttft_ms = (loop.time() - run_started - sent_at) * 1000

                    # 마지막 usage chunk에서 token·cache 지표 추출
                    usage = chunk.get("usage")
                    if usage:
                        vllm_prompt_tokens = usage.get("prompt_tokens")
                        completion_tokens = usage.get("completion_tokens")
                        details = usage.get("prompt_tokens_details") or {}
                        cached_tokens = details.get(
                                "cached_tokens", usage.get("num_cached_tokens")
                        )
    except Exception as exc:
        # 요청·응답 처리 예외 저장
        error = f"{type(exc).__name__}: {exc}"

    # 완료시각 및 prefix cache hit 비율 계산
    completed_at = loop.time() - run_started
    cache_hit_rate = None
    if vllm_prompt_tokens is not None and cached_tokens is not None:
        cached_tokens = max(0, min(cached_tokens, vllm_prompt_tokens))
        cache_hit_rate = (
            cached_tokens / vllm_prompt_tokens if vllm_prompt_tokens else 0.0
        )

    # 요청 식별자·측정 지표 결과 구성
    return {
        **{k: record[k] for k in ("workload", "request_id", "group_id", "step_id")},
        "tokenizer_prompt_tokens": record["prompt_token_len"],
        "vllm_prompt_tokens": vllm_prompt_tokens,
        "cached_tokens": cached_tokens,
        "cache_hit_rate": cache_hit_rate,
        "completion_tokens": completion_tokens,
        "ttft_ms": ttft_ms,
        "e2e_ms": (completed_at - sent_at) * 1000,
        "scheduled_at": scheduled_at,
        "sent_at": sent_at,
        "completed_at": completed_at,
        "queue_delay_ms": (sent_at - scheduled_at) * 1000,
        "status": "error" if error else "ok",
        "error": error,
    }


async def run_vllm(
    records: list[dict[str, Any]],
    config: dict[str, Any],
    workload: str,
) -> list[dict[str, Any]]:
    """선택 workload를 seed 기반 Poisson 스케줄과 동시성 상한에 따라 vLLM에 전송한다."""
    # 실행 대상 workload request 선택
    vllm = config["vllm"]
    selected = [record for record in records if record["workload"] == workload]

    # seed 기반 Poisson 도착 시간 생성
    rng = random.Random(vllm["schedule"]["seed"])
    schedule = [0.0]
    for _ in range(1, len(selected)):
        schedule.append(schedule[-1] + rng.expovariate(vllm["schedule"]["qps"]))

    # 실행 기준시각 및 HTTP 연결 수 상한 설정
    run_started = asyncio.get_running_loop().time()
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=vllm["timeout_seconds"]),
        connector=aiohttp.TCPConnector(limit=vllm["schedule"]["max_concurrency"]),
    ) as session:
        # request별 비동기 task 구성
        requests = [
            send_request(session, record, scheduled_at, run_started, config)
            for record, scheduled_at in zip(selected, schedule)
        ]

        # 완료 순서 기준 결과 수집 및 진행률 표시
        results = [
            await request
            for request in tqdm(
                asyncio.as_completed(requests), total=len(requests), desc="vllm"
            )
        ]

    # scheduled arrival 순서 기준 결과 정렬
    results.sort(key=lambda result: result["scheduled_at"])
    return results



# ---------------------------------------------------------------------------
# 결과 저장
# ---------------------------------------------------------------------------


def save_results(
    workload: str,
    results: list[dict[str, Any]],
    config: dict[str, Any],
    base_dir: Path,
) -> Path:
    """요청별 측정 결과를 config의 output directory에 JSONL로 저장한다."""
    # 결과 디렉터리 생성
    output_dir = base_dir / config["vllm"]["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    # 요청별 결과 JSONL 저장
    result_path = output_dir / f"{workload}_results.jsonl"
    with result_path.open("w", encoding="utf-8") as output_file:
        for result in results:
            output_file.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result_path

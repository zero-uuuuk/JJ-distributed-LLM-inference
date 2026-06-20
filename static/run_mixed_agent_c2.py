"""Case 2 / QuotaServe static — Chat(ShareGPT) + Agent(traj) mixed runner.

agent_validation/run_mixed_agent.py 를 그대로 복사·적응한 것이다. 클라이언트
동작은 Case 1과 동일하다 — QuotaServe static은 **서버측 env
(QUOTA_SERVE_MODE=static)**로 켜지므로 이 runner는 요청 전송/계측만 한다.
case2용 변경점은 세 가지뿐이다(run_mixed_c2.py와 동일).

  1. trace 경로: --workloads-root (기본 env JJ_ROOT/workloads) 기준.
  2. 출력: static/raw_results 에 c2_{quota_mode}_chat_agent_apc_on.jsonl.
  3. 메타데이터: summary에 quota_serve_mode 기록 (--quota-mode).

§8.4.2 (Chat + Agent)에 사용한다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable


np: Any = None


DEFAULT_MODEL = "meta-llama/Llama-3.2-3B-Instruct"
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_URL = "http://127.0.0.1:8000/v1/chat/completions"
DEFAULT_FALLBACK_MAX_TOKENS = 128
DEFAULT_AGENT_SLO_MS = 10000.0
DEFAULT_CHAT_SLO_MS = 400.0
DEFAULT_MAX_TOKENS_BY_WORKLOAD = {
    "chat": 691,
}

# static/run_mixed_agent_c2.py 위치 기준. 출력은 static/ 아래에 둔다.
STATIC_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = STATIC_DIR / "raw_results"

# workload trace는 JJ repo에 있다. --workloads-root 로 override 가능.
DEFAULT_JJ_ROOT = Path(
    os.environ.get("JJ_ROOT", Path.home() / "JJ-distributed-LLM-inference")
)
DEFAULT_WORKLOADS_ROOT = DEFAULT_JJ_ROOT / "workloads"


def load_runtime_dependencies() -> tuple[Any, Any]:
    global np
    try:
        import aiohttp
        import numpy as numpy_module
        from tqdm import tqdm
    except ImportError as exc:
        raise SystemExit(
            "Missing runtime dependency. Activate the project venv or install requirements.txt "
            f"({type(exc).__name__}: {exc})."
        ) from exc
    np = numpy_module
    return aiohttp, tqdm


# ---------------------------------------------------------------------------
# JSONL IO
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved_path = path.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    with resolved_path.open("r", encoding="utf-8") as input_file:
        for line in input_file:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as output_file:
        json.dump(data, output_file, ensure_ascii=False, indent=2)


def resolve_summary_path(output_path: Path) -> Path:
    resolved_output = output_path.expanduser().resolve()
    return resolved_output.with_name(f"{resolved_output.stem}_summary.json")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Case 2/QuotaServe static — Chat + trajectory Agent mixed runner.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--workloads-root",
        type=Path,
        default=DEFAULT_WORKLOADS_ROOT,
        help="JJ repo의 workloads/ 디렉토리 (env JJ_ROOT/workloads 기본)",
    )
    parser.add_argument(
        "--quota-mode",
        default=os.environ.get("QUOTA_SERVE_MODE", "off"),
        help="결과 메타데이터/파일명에 기록할 QuotaServe 모드 (라벨링용).",
    )
    parser.add_argument("--agent-trace", type=Path, default=None)
    parser.add_argument("--chat-trace", type=Path, default=None)
    parser.add_argument(
        "--phase",
        choices=("chat_agent_fixed", "chat_agent_gaussian", "chat_agent_exponential"),
        default="chat_agent_exponential",
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
    parser.add_argument("--chat-qps", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-concurrency", type=int, default=32)
    parser.add_argument("--num-agent-prompts", type=int, default=1000)
    parser.add_argument("--num-chat-prompts", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--agent-slo-ms", type=float, default=DEFAULT_AGENT_SLO_MS)
    parser.add_argument("--chat-slo-ms", type=float, default=DEFAULT_CHAT_SLO_MS)
    return parser.parse_args()


def resolve_trace_path(path: Path, label: str, build_hint: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        return resolved
    raise SystemExit(
        f"{label} trace not found: {resolved}\n"
        f"Check --workloads-root or build it first:\n{build_hint}"
    )


def resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output is not None:
        return args.output.expanduser().resolve()
    filename = f"c2_{args.quota_mode}_chat_agent_apc_on.jsonl"
    return (DEFAULT_OUTPUT_DIR / filename).resolve()


def resolve_gap_mode(phase: str, explicit_mode: str | None) -> str:
    if explicit_mode is not None:
        return explicit_mode
    if "exponential" in phase:
        return "exponential"
    if "gaussian" in phase:
        return "gaussian"
    return "fixed"


# ---------------------------------------------------------------------------
# Metrics utilities
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
# Payload and request sending
# ---------------------------------------------------------------------------


def resolve_max_token_cap(workload_tag: str) -> int | None:
    return DEFAULT_MAX_TOKENS_BY_WORKLOAD.get(workload_tag.lower())


def resolve_max_tokens(row: dict[str, Any], workload_tag: str) -> int:
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
    if row.get("messages") is not None:
        return row["messages"]
    if row.get("prompt") is not None:
        return [{"role": "user", "content": row["prompt"]}]
    raise ValueError("row must contain messages or prompt")


def build_payload(
    row: dict[str, Any],
    model: str,
    max_tokens: int,
    workload_tag: str,
) -> dict[str, Any]:
    # 'user' 필드로 workload 태그 전달 — QuotaServe PR 2(user -> Request.workload_id).
    return {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": messages_from_row(row),
        "user": workload_tag,
    }


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
    semaphore: asyncio.Semaphore,
    workload_tag: str,
) -> dict[str, Any]:
    actual_max_tokens = resolve_max_tokens(row, workload_tag)
    payload = build_payload(row, model, actual_max_tokens, workload_tag)
    # 스케줄링 코루틴이 이 요청을 "보내기로 한 시각". 실제 전송(semaphore 획득)
    # 까지의 차이가 queue_delay(클라이언트측 backpressure)다.
    scheduled_perf = row.get("_scheduled_perf")
    scheduled_wall = row.get("_scheduled_wall")

    # semaphore로 동시 in-flight 수를 max_concurrency로 제한.
    async with semaphore:
        start_perf = time.perf_counter()  # 단조 시계: 지연 계산용
        start_wall = time.time()          # 벽시계: 로그 타임스탬프
        last_token_perf = start_perf
        ttft: float | None = None         # time-to-first-token
        itls: list[float] = []            # inter-token latency 샘플
        prompt_tokens = None
        completion_tokens = None
        cached_tokens = None
        error: str | None = None

        try:
            # SSE 스트리밍(stream=True + include_usage): 토큰이 "data: {...}"로
            # 하나씩 오고, 끝에 usage(캐시 hit 포함) 청크가 온다.
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    error = f"http {response.status}: {(await response.text())[:200]}"
                else:
                    async for raw_line in response.content:
                        line = raw_line.decode("utf-8").strip()
                        if not line.startswith("data:"):
                            continue  # SSE "data:" 라인만 의미 있음
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break  # 스트림 종료
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue  # 부분/깨진 청크는 건너뜀
                        usage = chunk.get("usage")
                        if usage:
                            # 스트림 끝 usage 청크: 토큰 수 + 캐시 hit 토큰.
                            prompt_tokens = usage.get("prompt_tokens")
                            completion_tokens = usage.get("completion_tokens")
                            cached_tokens = extract_cached_tokens(usage)
                        text = extract_stream_text(chunk)
                        if not text:
                            continue  # 텍스트 없는 청크는 타이밍 제외
                        now_perf = time.perf_counter()
                        if ttft is None:
                            ttft = now_perf - start_perf  # 첫 토큰 = TTFT
                        else:
                            itls.append(now_perf - last_token_perf)  # 토큰 간격 = ITL
                        last_token_perf = now_perf
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"  # 요청 단위 예외 격리

        end_perf = time.perf_counter()
        end_wall = time.time()

    mean_itl = float(np.mean(itls)) if itls else None
    prompt_token_count = int(prompt_tokens) if isinstance(prompt_tokens, (int, float)) else None
    cached_token_count = int(cached_tokens) if isinstance(cached_tokens, (int, float)) else None

    # prefix cache hit rate. cached_tokens가 prompt를 초과 보고될 수 있어 클램프.
    h_r = u_r = hit_rate = None
    if prompt_token_count is not None and cached_token_count is not None:
        h_r = max(0, min(cached_token_count, prompt_token_count))
        u_r = prompt_token_count - h_r
        hit_rate = h_r / prompt_token_count if prompt_token_count > 0 else 0.0

    # queue_delay = 보내기로 한 시각 → 실제 전송 시각 (클라이언트측 대기).
    schedule_delay = None
    if isinstance(scheduled_perf, (int, float)):
        schedule_delay = start_perf - float(scheduled_perf)

    return {
        "workload": workload_tag,
        "phase": row.get("_phase"),
        "request_id": row.get("request_id"),
        "session_id": row.get("session_id"),
        "step_id": row.get("step_id"),
        "task_id": row.get("task_id"),
        "conversation_id": row.get("conversation_id"),
        "turn_id": row.get("turn_id"),
        "tool_name": row.get("tool_name"),
        "tool_gap_seconds": row.get("_tool_gap_seconds", row.get("tool_gap_seconds")),
        "scheduled_wall": scheduled_wall,
        "dispatch_wall": start_wall,
        "queue_delay": schedule_delay,
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
# Scheduling
# ---------------------------------------------------------------------------


def group_agent_sessions(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """평탄한 agent 요청 리스트를 세션별로 묶고, 각 세션을 step 순서로 정렬한다.

    agent trace는 (session_id, step_id)로 구성된다. 한 세션 = 한 에이전트의
    연속된 tool-call step들이며, 같은 세션의 step들은 **공통 prefix를 공유**한다
    (앞 step의 대화/관찰이 뒤 step의 prompt에 누적). 이 prefix 재사용이 바로
    agent의 "delayed self-reuse"(§8.4.2 warm antagonist)의 원천이라, 세션 단위로
    순서를 지켜 보내는 것이 중요하다. session_id가 없으면 fallback으로 합성한다.
    """
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
        # step_id(없으면 turn_id) 오름차순 — 세션 내 prefix 누적 순서를 보존.
        session_rows.sort(key=lambda item: int(item.get("step_id", item.get("turn_id", 0)) or 0))
    return sessions


def session_start_delays(num_sessions: int, session_start_rps: float, seed: int) -> list[float]:
    """각 세션의 "시작 시각"(t=0 기준 오프셋)을 Poisson 과정으로 생성한다.

    세션은 step 단위가 아니라 세션 단위로 도착한다. session_start_rps는
    agent_target_rps / steps_per_session로 정해지는데(아래 run_agent_workload),
    이는 "step 기준 목표 RPS"를 "세션 도착률"로 환산한 것이다 — 한 세션이
    steps_per_session개의 요청을 만들기 때문. 누적 exponential 간격으로 세션
    시작 시각을 벌려, 여러 에이전트가 시차를 두고 진입하는 상황을 만든다.
    """
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
    """한 step이 끝난 뒤 다음 step까지의 "tool 실행 간격"을 샘플링한다.

    실제 에이전트는 step 사이에 도구(쉘/검색 등)를 실행하느라 think-time gap이
    생긴다. 이 gap 동안 세션의 cached prefix가 LRU로 밀려날 수 있고, 다음 step에서
    그 prefix가 다시 필요해지면 useful eviction(피해)이 된다 — 이게 agent를 측정
    대상으로 삼는 핵심 메커니즘이다.
      - fixed:       항상 같은 간격(재현용 baseline)
      - exponential: 평균 mean, max로 상한(메인 설정; 긴 꼬리 gap 재현)
      - gaussian:    평균 mean·표준편차 std, [min, max]로 클립
    """
    if mode == "fixed":
        return max(0.0, float(fixed_seconds))
    if mode == "exponential":
        sampled = float(rng.exponential(mean_seconds))
        return min(sampled, float(max_seconds))
    sampled = float(rng.normal(mean_seconds, std_seconds))
    return max(float(min_seconds), min(sampled, float(max_seconds)))


async def record_result(
    result: dict[str, Any],
    results: list[dict[str, Any]],
    progress_bar: tqdm,
    progress_lock: asyncio.Lock,
) -> None:
    async with progress_lock:
        results.append(result)
        status = "ERR" if result["error"] else "OK"
        ttft_text = f"{result['ttft'] * 1000:.0f}ms" if result["ttft"] else "-"
        progress_bar.set_postfix(
            workload=result["workload"],
            status=status,
            ttft=ttft_text,
        )
        progress_bar.update(1)


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
    phase: str,
    gap_mode: str,
    fixed_gap_seconds: float,
    gap_mean: float,
    gap_std: float,
    gap_min: float,
    gap_max: float,
) -> None:
    # 세션 시작 시각까지 대기(세션마다 다른 오프셋 → 시차 진입).
    if start_delay > 0:
        await asyncio.sleep(start_delay)

    # 세션별 독립 RNG(세션 인덱스로 seed) — tool-gap 시퀀스를 재현 가능하게.
    rng = np.random.default_rng(10_000 + session_index)
    previous_gap: float | None = None

    # 세션 내 step을 **순차적으로** 보낸다(앞 step 완료 후 다음 step). 이래야
    # step 간 prefix 누적·재사용이 실제 에이전트처럼 재현된다.
    for index, request in enumerate(session_rows):
        row = {
            **request,
            "_workload": "agent",
            "_phase": phase,
            "_tool_gap_seconds": previous_gap,         # 직전 gap 기록(분석용)
            "_scheduled_perf": time.perf_counter(),    # queue_delay 측정 기준
            "_scheduled_wall": time.time(),
        }
        result = await send_one(session, url, model, row, semaphore, "agent")
        await record_result(result, results, progress_bar, progress_lock)

        # 마지막 step이 아니면 tool 실행 gap만큼 쉰 뒤 다음 step. 이 gap 동안
        # cache가 밀려나면 다음 step에서 self-reuse 피해가 발생할 수 있다.
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
    phase: str,
    gap_mode: str,
    fixed_gap_seconds: float,
    gap_mean: float,
    gap_std: float,
    gap_min: float,
    gap_max: float,
) -> None:
    # 1) 평탄 리스트를 세션으로 묶고, 2) 세션 도착률을 계산해, 3) 세션마다
    #    시작 오프셋을 부여한 뒤, 4) 세션을 동시 코루틴으로 띄운다. 각 세션은
    #    내부적으로 step을 순차 전송하므로, 세션끼리는 병렬·세션 안은 직렬이다.
    sessions = group_agent_sessions(agent_requests)
    # 목표 step RPS를 세션 도착률로 환산: 한 세션이 steps_per_session개를 만든다.
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
                phase=phase,
                gap_mode=gap_mode,
                fixed_gap_seconds=fixed_gap_seconds,
                gap_mean=gap_mean,
                gap_std=gap_std,
                gap_min=gap_min,
                gap_max=gap_max,
            )
        )
        for index, session_rows in enumerate(sessions)
    ]
    if tasks:
        await asyncio.gather(*tasks)


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
    phase: str,
) -> None:
    # Chat 워크로드 드라이버: agent와 달리 세션 개념 없이 Poisson 도착으로
    # 요청을 fire-and-forget 한다(각 요청을 즉시 task로 띄움). chat은 victim이라
    # 독립적인 도착이 필요하다 — agent와 동시에 돌며 cache를 두고 경쟁한다.
    rng = np.random.default_rng(seed)
    tasks: list[asyncio.Task[None]] = []

    async def send_and_record(row: dict[str, Any]) -> None:
        result = await send_one(session, url, model, row, semaphore, workload_tag)
        await record_result(result, results, progress_bar, progress_lock)

    for request in requests:
        row = {
            **request,
            "_workload": workload_tag,
            "_phase": phase,
            "_scheduled_perf": time.perf_counter(),
            "_scheduled_wall": time.time(),
        }
        # 도착 즉시 전송 task 생성(블로킹하지 않음). 동시성은 semaphore가 제어.
        tasks.append(asyncio.create_task(send_and_record(row)))
        if qps > 0:
            await asyncio.sleep(float(rng.exponential(1.0 / qps)))  # Poisson 간격

    if tasks:
        await asyncio.gather(*tasks)  # 모든 chat 요청 완료 대기


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def summarize_workload(
    rows: list[dict[str, Any]],
    tag: str,
    slo_ms: float | None,
    duration_seconds: float,
) -> dict[str, Any]:
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
    # SLO attainment = (TTFT ≤ SLO 성공 수) / (성공 + 실패). 분모에 실패 포함이라
    # 타임아웃도 위반으로 계산. agent는 SLO가 10s로 chat(400ms)보다 느슨하다(§2).
    slo_attainment = (
        sum(1 for t in ttfts if t <= slo_ms) / (len(ok) + len(failed))
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


def build_summary(
    results: list[dict[str, Any]],
    duration_seconds: float,
    phase: str,
    agent_slo_ms: float,
    chat_slo_ms: float | None,
    scheduler: dict[str, Any],
    quota_mode: str,
) -> dict[str, Any]:
    total_ok = sum(1 for r in results if r["error"] is None and r["ttft"] is not None)
    total_failed = len(results) - total_ok
    return {
        # case2 메타데이터: 어떤 QuotaServe 모드로 측정했는지 기록.
        "quota_serve_mode": quota_mode,
        "phase": phase,
        "duration_seconds": duration_seconds,
        "n_total": len(results),
        "n_ok": total_ok,
        "n_failed": total_failed,
        "scheduler": scheduler,
        "agent": summarize_workload(results, "agent", agent_slo_ms, duration_seconds),
        "chat": summarize_workload(results, "chat", chat_slo_ms, duration_seconds),
    }


def print_summary(summary: dict[str, Any], workload_tags: list[str]) -> None:
    print(f"\n{'=' * 50}")
    print(f"quota_serve_mode={summary['quota_serve_mode']}")
    print(
        f"phase={summary['phase']}  total={summary['n_total']}  "
        f"ok={summary['n_ok']}  failed={summary['n_failed']}  "
        f"duration={summary['duration_seconds']:.1f}s"
    )

    for tag in workload_tags:
        workload_summary = summary[tag]
        if workload_summary["n_ok"] == 0:
            print(f"\n  [{tag.upper()}] no successful requests (failed={workload_summary['n_failed']})")
            continue

        print(
            f"\n  [{tag.upper()}] ok={workload_summary['n_ok']} "
            f"failed={workload_summary['n_failed']}"
        )
        print(
            f"  throughput: {workload_summary['throughput_req_per_s']:.2f} req/s  "
            f"{workload_summary['throughput_tok_per_s']:.1f} tok/s"
        )
        if workload_summary["slo_attainment"] is not None:
            print(
                f"  SLO attainment (TTFT <= {workload_summary['slo_ms']:.0f}ms): "
                f"{workload_summary['slo_attainment']:.1%}"
            )
        if workload_summary["hit_rate_mean"] == workload_summary["hit_rate_mean"]:
            print(
                f"  cache hit rate: mean={workload_summary['hit_rate_mean']:.3f} "
                f"p50={workload_summary['hit_rate_p50']:.3f}"
            )
        print(
            f"  TTFT(ms): mean={workload_summary['ttft_ms_mean']:.1f} "
            f"p50={workload_summary['ttft_ms_p50']:.1f} "
            f"p95={workload_summary['ttft_ms_p95']:.1f} "
            f"p99={workload_summary['ttft_ms_p99']:.1f}"
        )
        if workload_summary["queue_delay_ms_mean"] == workload_summary["queue_delay_ms_mean"]:
            print(
                f"  queue delay(ms): mean={workload_summary['queue_delay_ms_mean']:.1f} "
                f"p95={workload_summary['queue_delay_ms_p95']:.1f}"
            )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    aiohttp, tqdm = load_runtime_dependencies()

    gap_mode = resolve_gap_mode(args.phase, args.agent_tool_gap_mode)

    agent_trace_path = (
        args.agent_trace
        if args.agent_trace is not None
        else args.workloads_root / "traj/traj_agent_100session_10step.jsonl"
    )
    chat_trace_path = (
        args.chat_trace
        if args.chat_trace is not None
        else args.workloads_root / "sharegpt/sharegpt_victim_100conv_10turn.jsonl"
    )
    agent_trace = resolve_trace_path(
        agent_trace_path,
        "Agent",
        "  cd workloads/traj && python build_traj_agent_workload.py "
        "--num-sessions 100 --max-steps 10 --min-steps 10 "
        "--output traj_agent_100session_10step.jsonl --tokenizer approx",
    )
    chat_trace = resolve_trace_path(
        chat_trace_path,
        "Chat",
        "  cd workloads/sharegpt && python build_sharegpt_workload.py "
        "--num-conversations 100 --min-turns 10 --max-turns 10 "
        "--order turn-major --output sharegpt_victim_100conv_10turn.jsonl",
    )
    output_path = resolve_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    agent_requests = load_jsonl(agent_trace)
    chat_requests = load_jsonl(chat_trace)
    if args.num_agent_prompts > 0:
        agent_requests = agent_requests[: args.num_agent_prompts]
    if args.num_chat_prompts > 0:
        chat_requests = chat_requests[: args.num_chat_prompts]

    total = len(agent_requests) + len(chat_requests)
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
        "chat_qps": args.chat_qps,
    }

    print(
        f"quota_mode={args.quota_mode} | phase={args.phase} | chat={len(chat_requests)} "
        f"@ {args.chat_qps} qps | agent={len(agent_requests)} "
        f"@ target {args.agent_target_rps} rps | total={total} "
        f"| concurrency={args.max_concurrency}"
    )
    print(f"chat_trace={chat_trace}")
    print(f"agent_trace={agent_trace}")
    print(f"gap_mode={gap_mode}")
    print(f"output={output_path}")

    semaphore = asyncio.Semaphore(args.max_concurrency)
    timeout = aiohttp.ClientTimeout(total=args.timeout_seconds)
    results: list[dict[str, Any]] = []
    progress_lock = asyncio.Lock()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        start_perf = time.perf_counter()
        with tqdm(total=total, unit="req", dynamic_ncols=True) as progress_bar:
            # 두 워크로드를 동시에 구동한다: agent(세션 단위, tool-gap 있음) ∥
            # chat(Poisson, victim). 같은 KV cache를 두고 경쟁시켜 antagonist가
            # victim의 prefix를 오염시키는 상황을 재현한다(§8.4.2).
            await asyncio.gather(
                run_agent_workload(
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
                ),
                run_poisson_workload(
                    requests=chat_requests,
                    qps=args.chat_qps,
                    workload_tag="chat",
                    seed=43,
                    session=session,
                    url=args.url,
                    model=args.model,
                    semaphore=semaphore,
                    results=results,
                    progress_bar=progress_bar,
                    progress_lock=progress_lock,
                    phase=args.phase,
                ),
            )
        duration_seconds = time.perf_counter() - start_perf

    summary = build_summary(
        results=results,
        duration_seconds=duration_seconds,
        phase=args.phase,
        agent_slo_ms=args.agent_slo_ms,
        chat_slo_ms=args.chat_slo_ms,
        scheduler=scheduler,
        quota_mode=args.quota_mode,
    )
    summary_path = resolve_summary_path(output_path)
    write_jsonl(output_path, results)
    write_json(summary_path, summary)
    print_summary(summary, ["chat", "agent"])
    print(f"\nsaved: {output_path}")
    print(f"summary: {summary_path}")


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

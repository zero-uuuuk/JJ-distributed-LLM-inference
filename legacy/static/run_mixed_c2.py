"""Case 2 / QuotaServe static — Chat(ShareGPT) + Longctx/RAG mixed runner.

case1_validation/run_mixed.py 를 그대로 복사·적응한 것이다. 클라이언트 동작은
Case 1과 동일하다 — QuotaServe static은 **서버측 env(QUOTA_SERVE_MODE=static)**로
켜지므로 이 runner는 요청 전송/계측만 한다. case2용 변경점은 세 가지뿐이다.

  1. trace 경로: quotaserve repo는 JJ repo와 분리돼 있어, workload trace는
     --workloads-root (기본 env JJ_ROOT/workloads)에서 찾는다.
  2. 출력: 기본 저장 위치를 static/raw_results 로, 파일명을
     c2_{quota_mode}_chat_{antagonist}_apc_on.jsonl 로 둔다.
  3. 메타데이터: 어떤 QuotaServe 모드로 돌렸는지 summary에 기록한다
     (--quota-mode, 기본 env QUOTA_SERVE_MODE).

§8.4.1 (Chat + Longctx)에 사용한다. Longctx로 돌리려면 --longctx-trace를 준다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

import aiohttp
import numpy as np
from tqdm import tqdm


DEFAULT_MODEL = "meta-llama/Llama-3.2-3B-Instruct"
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_CHAT_SLO_MS = 400.0
DEFAULT_RAG_SLO_MS = 400.0
DEFAULT_LONGCTX_SLO_MS = 7700.0
DEFAULT_URL = "http://127.0.0.1:8000/v1/chat/completions"
DEFAULT_FALLBACK_MAX_TOKENS = 128
DEFAULT_MAX_OUTPUT_TOKENS = 1776

# static/run_mixed_c2.py 위치 기준. 출력은 static/ 아래에 둔다.
STATIC_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = STATIC_DIR / "raw_results"

# workload trace는 JJ repo에 있다. --workloads-root 로 override 가능.
# 기본값: env JJ_ROOT/workloads, 없으면 ~/JJ-distributed-LLM-inference/workloads
#   (RUN_MIXED.md의 /home/ubuntu/... 리눅스 실행 환경 기준).
DEFAULT_JJ_ROOT = Path(
    os.environ.get("JJ_ROOT", Path.home() / "JJ-distributed-LLM-inference")
)
DEFAULT_WORKLOADS_ROOT = DEFAULT_JJ_ROOT / "workloads"


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
        description="Case 2/QuotaServe static — Chat + RAG/Longctx mixed runner.",
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
        help="결과 메타데이터/파일명에 기록할 QuotaServe 모드. 실제 정책은 "
        "서버측 env로 켜진다(이 값은 라벨링용).",
    )
    parser.add_argument(
        "--chat-trace",
        type=Path,
        default=None,
        help="ShareGPT JSONL 경로 (기본: workloads-root 기준)",
    )
    parser.add_argument(
        "--rag-trace",
        type=Path,
        default=None,
        help="RAG JSONL 경로 (기본: workloads-root 기준 MS MARCO)",
    )
    parser.add_argument(
        "--longctx-trace",
        type=Path,
        nargs="?",
        const="__default__",
        default=None,
        help=(
            "Longctx JSONL 경로. 지정하면 RAG 대신 Longctx mixed를 실행한다. "
            "값 없이 지정하면 workloads-root 기준 기본 HotpotQA longctx trace를 쓴다."
        ),
    )
    parser.add_argument("--chat-qps", type=float, default=5.0, help="Chat 도착 QPS")
    parser.add_argument("--rag-qps", type=float, default=5.0, help="RAG 도착 QPS")
    parser.add_argument("--longctx-qps", type=float, default=5.0, help="Longctx 도착 QPS")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="결과 JSONL 저장 경로. 생략하면 quota-mode와 antagonist를 반영해 자동 생성.",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="vLLM chat completions URL")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-concurrency", type=int, default=32)
    parser.add_argument("--num-chat-prompts", type=int, default=1000)
    parser.add_argument("--num-rag-prompts", type=int, default=1000)
    parser.add_argument("--num-longctx-prompts", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--chat-slo-ms", type=float, default=DEFAULT_CHAT_SLO_MS)
    parser.add_argument("--rag-slo-ms", type=float, default=DEFAULT_RAG_SLO_MS)
    parser.add_argument("--longctx-slo-ms", type=float, default=DEFAULT_LONGCTX_SLO_MS)
    return parser.parse_args()


def resolve_trace_path(path: Path, label: str, build_hint: str) -> Path:
    """trace 경로를 절대 경로로 변환하고, 없으면 생성 방법을 안내한다."""
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        return resolved
    raise SystemExit(
        f"{label} trace를 찾을 수 없습니다: {resolved}\n"
        f"--workloads-root 를 확인하거나 먼저 워크로드를 생성하세요:\n{build_hint}"
    )


def format_qps_label(qps: float) -> str:
    """파일명에 넣을 QPS 값을 짧고 안전한 문자열로 변환한다."""
    if float(qps).is_integer():
        return str(int(qps))
    return str(qps).replace("-", "m").replace(".", "p")


def resolve_output_path(
    args: argparse.Namespace, antagonist_tag: str
) -> Path:
    """명시된 output이 없으면 quota-mode와 antagonist가 드러나는 기본 파일명을 만든다."""
    if args.output is not None:
        return args.output.expanduser().resolve()
    filename = f"c2_{args.quota_mode}_chat_{antagonist_tag}_apc_on.jsonl"
    return (DEFAULT_OUTPUT_DIR / filename).resolve()


def resolve_antagonist_config(args: argparse.Namespace) -> dict[str, Any]:
    """Longctx trace가 지정되면 RAG 대신 Longctx를 두 번째 workload로 선택한다."""
    workloads_root: Path = args.workloads_root

    if args.longctx_trace is not None:
        trace = (
            workloads_root / "hotpotqa/hotpotqa_longctx_2000_4000.jsonl"
            if str(args.longctx_trace) == "__default__"
            else args.longctx_trace
        )
        return {
            "tag": "longctx",
            "label": "Longctx",
            "trace": trace,
            "qps": args.longctx_qps,
            "num_prompts": args.num_longctx_prompts,
            "slo_ms": args.longctx_slo_ms,
            "build_hint": (
                "  cd workloads/hotpotqa && python build_hotpotqa_workload.py "
                "--output hotpotqa_longctx_2000_4000.jsonl"
            ),
        }

    rag_trace = (
        args.rag_trace
        if args.rag_trace is not None
        else workloads_root / "msmarco/msmarco_v21_validation.jsonl"
    )
    return {
        "tag": "rag",
        "label": "RAG",
        "trace": rag_trace,
        "qps": args.rag_qps,
        "num_prompts": args.num_rag_prompts,
        "slo_ms": args.rag_slo_ms,
        "build_hint": (
            "  cd workloads/msmarco && python build_msmarco_rag_workload.py "
            "--output msmarco_v21_validation.jsonl"
        ),
    }


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


def resolve_max_tokens(row: dict[str, Any], workload_tag: str) -> int:
    """요청별 output_token_len을 보존하되 전역 생성 상한 1776을 넘기지 않는다."""
    row_max_tokens = row.get("output_token_len", row.get("output_tokens"))
    token_cap = DEFAULT_MAX_OUTPUT_TOKENS

    if row_max_tokens is None:
        return DEFAULT_FALLBACK_MAX_TOKENS
    return int(min(row_max_tokens, token_cap))


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
    vLLM은 이 값을 ChatCompletionRequest.user로 파싱하며, QuotaServe PR 2의
    workload tag 전파(user -> Request.workload_id)의 입력이 된다.
    """
    return {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": messages_from_row(row),
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
    semaphore: asyncio.Semaphore,
    workload_tag: str,
) -> dict[str, Any]:
    actual_max_tokens = resolve_max_tokens(row, workload_tag)
    payload = build_payload(row, model, actual_max_tokens, workload_tag=workload_tag)

    # semaphore로 동시 in-flight 요청 수를 max_concurrency로 제한한다(서버 과부하
    # 방지). 측정 구간은 semaphore 획득 직후부터 — 즉 큐 대기 시간은 TTFT에
    # 포함하지 않고 순수 서버 응답 지연만 본다.
    async with semaphore:
        start_perf = time.perf_counter()  # 단조 시계: 지연(TTFT/ITL) 계산용
        start_wall = time.time()          # 벽시계: 로그 타임스탬프용
        last_token_perf = start_perf
        ttft: float | None = None         # time-to-first-token
        itls: list[float] = []            # inter-token latency 샘플들
        prompt_tokens = None
        completion_tokens = None
        cached_tokens = None
        error: str | None = None

        try:
            # SSE 스트리밍 응답. stream=True + include_usage라서 토큰이 하나씩
            # "data: {...}" 라인으로 오고, 마지막에 usage(캐시 hit 포함) 청크가 온다.
            # QuotaServe PR 2: workload 태그를 X-Request-Id로 실어 보낸다. vLLM은
            # 이 헤더를 request_id로 반영(chatcmpl-<태그>-<uuid>)하고, 서버측
            # collector가 request_id에서 workload를 추론해 block owner로 쓴다.
            headers = {"X-Request-Id": f"{workload_tag}-{uuid.uuid4().hex}"}
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status != 200:
                    # 비정상 응답은 본문 앞 200자만 잘라 에러로 기록(로그 폭주 방지).
                    error = f"http {response.status}: {(await response.text())[:200]}"
                else:
                    async for raw_line in response.content:
                        line = raw_line.decode("utf-8").strip()
                        # SSE는 "data:" 접두 라인만 의미가 있다. 그 외(빈 줄 등) 무시.
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break  # 스트림 종료 신호
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue  # 부분 청크 등 파싱 실패는 건너뛴다
                        # usage 청크: prompt/completion 토큰 수와 캐시 hit 토큰 수.
                        # include_usage 덕에 스트림 끝에 한 번 도착한다.
                        usage = chunk.get("usage")
                        if usage:
                            prompt_tokens = usage.get("prompt_tokens")
                            completion_tokens = usage.get("completion_tokens")
                            cached_tokens = extract_cached_tokens(usage)
                        text = extract_stream_text(chunk)
                        if not text:
                            # delta 텍스트가 없는 청크(usage-only 등)는 타이밍에서 제외.
                            continue
                        now_perf = time.perf_counter()
                        if ttft is None:
                            # 첫 토큰 도착 = TTFT(prefill + 첫 디코드 지연).
                            ttft = now_perf - start_perf
                        else:
                            # 이후 토큰 간격 = ITL(디코드 단계 지연).
                            itls.append(now_perf - last_token_perf)
                        last_token_perf = now_perf
        except Exception as exc:
            # 타임아웃/연결 끊김 등 모든 예외를 문자열로 보존(요청 단위로 격리).
            error = f"{type(exc).__name__}: {exc}"

        end_perf = time.perf_counter()
        end_wall = time.time()

    mean_itl = float(np.mean(itls)) if itls else None
    prompt_token_count = int(prompt_tokens) if isinstance(prompt_tokens, (int, float)) else None
    cached_token_count = int(cached_tokens) if isinstance(cached_tokens, (int, float)) else None

    # prefix cache hit rate 계산.
    #   h_r = hit tokens(캐시로 채운 prompt 토큰), u_r = miss(새로 계산), hit_rate = h_r/prompt.
    # vLLM 버전에 따라 cached_tokens가 prompt_tokens보다 크게 보고될 수 있어
    # [0, prompt_token_count]로 클램프해 hit_rate가 1을 넘지 않게 한다.
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
    """독립적인 Poisson 도착 과정으로 요청을 공유 큐에 투입한다.

    워크로드마다 별도 seed의 RNG로 exponential(1/qps) 간격을 두어 도착시킨다.
    이렇게 두 워크로드(chat, antagonist)가 서로 독립적으로 도착해야, 실제
    운영처럼 antagonist 트래픽이 chat의 cache를 오염시키는 상황을 재현한다.
    `_workload` 키로 태그를 실어 consumer가 어느 워크로드인지 구분하게 한다.
    """
    rng = np.random.default_rng(seed)
    for request in requests:
        await queue.put({**request, "_workload": workload_tag})
        if qps > 0:
            # Poisson 과정 = 도착 간격이 지수분포. qps=0이면 간격 없이 전부 투입.
            await asyncio.sleep(rng.exponential(1.0 / qps))


async def consumer(
    queue: asyncio.Queue,
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    semaphore: asyncio.Semaphore,
    results: list[dict[str, Any]],
    progress_bar: tqdm,
) -> None:
    """큐에서 요청을 꺼내 서버로 전송하는 소비자 코루틴.

    max_concurrency 개가 동시에 돌며 큐를 비운다. `None`은 종료 sentinel이다 —
    하나를 받으면 다시 큐에 넣어 다른 consumer들도 차례로 종료하게 한다(릴레이).
    """
    while True:
        row = await queue.get()
        if row is None:
            await queue.put(None)  # 다음 consumer를 위해 sentinel 재투입 후 종료
            return
        workload_tag = row.pop("_workload", "unknown")
        result = await send_one(session, url, model, row, semaphore, workload_tag)
        results.append(result)
        # 진행바에 마지막 요청의 워크로드/성공여부/TTFT를 표시(실시간 모니터링).
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
) -> dict[str, Any]:
    ok = [r for r in rows if r["workload"] == tag and r["error"] is None and r["ttft"] is not None]
    failed = [r for r in rows if r["workload"] == tag and (r["error"] is not None or r["ttft"] is None)]

    if not ok:
        return {
            "workload": tag,
            "n_total": len(ok) + len(failed),
            "n_ok": len(ok),
            "n_failed": len(failed),
            "throughput_req_per_s": 0.0,
            "throughput_tok_per_s": 0.0,
            "slo_ms": slo_ms,
            "slo_attainment": 0.0 if (ok or failed) else float("nan"),
            "hit_rate_mean": float("nan"),
            "hit_rate_p50": float("nan"),
            "ttft_ms_mean": float("nan"),
            "ttft_ms_p50": float("nan"),
            "ttft_ms_p95": float("nan"),
            "ttft_ms_p99": float("nan"),
            "tpot_ms_mean": float("nan"),
            "tpot_ms_p50": float("nan"),
            "tpot_ms_p95": float("nan"),
            "tpot_ms_p99": float("nan"),
            "failed_examples": [
                {"request_id": r.get("request_id"), "error": r.get("error")}
                for r in failed[:3]
            ],
        }

    # ms 단위로 변환해 집계. TTFT가 핵심 지표, TPOT/hit_rate는 보조.
    ttfts = [r["ttft"] * 1000 for r in ok]
    tpots = [r["tpot"] * 1000 for r in ok if r["tpot"] is not None]
    hit_rates = [r["hit_rate"] for r in ok if r["hit_rate"] is not None]
    total_output = sum((r["completion_tokens"] or 0) for r in ok)
    # SLO attainment = (TTFT ≤ SLO인 성공 요청 수) / (성공 + 실패). 분모에 실패를
    # 포함해, 타임아웃/에러도 SLO 위반으로 정직하게 계산한다.
    slo_attainment = sum(1 for t in ttfts if t <= slo_ms) / (len(ok) + len(failed))

    return {
        "workload": tag,
        "n_total": len(ok) + len(failed),
        "n_ok": len(ok),
        "n_failed": len(failed),
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
        "failed_examples": [
            {"request_id": r.get("request_id"), "error": r.get("error")}
            for r in failed[:3]
        ],
    }


def build_summary(
    results: list[dict[str, Any]],
    duration_seconds: float,
    chat_slo_ms: float,
    antagonist_tag: str,
    antagonist_slo_ms: float,
    quota_mode: str,
) -> dict[str, Any]:
    """콘솔 출력과 파일 저장에 함께 쓰는 mixed 집계 지표를 생성한다."""
    total_ok = sum(1 for r in results if r["error"] is None and r["ttft"] is not None)
    total_failed = len(results) - total_ok
    chat_summary = summarize_workload(results, "chat", chat_slo_ms, duration_seconds)
    antagonist_summary = summarize_workload(results, antagonist_tag, antagonist_slo_ms, duration_seconds)

    return {
        # case2 메타데이터: 어떤 QuotaServe 모드로 측정했는지 기록.
        "quota_serve_mode": quota_mode,
        "duration_seconds": duration_seconds,
        "n_total": len(results),
        "n_ok": total_ok,
        "n_failed": total_failed,
        "antagonist_workload": antagonist_tag,
        "chat": chat_summary,
        antagonist_tag: antagonist_summary,
    }


def print_summary(summary: dict[str, Any]) -> None:
    print(f"\n{'=' * 50}")
    print(f"quota_serve_mode={summary['quota_serve_mode']}")
    print(
        f"전체: {summary['n_total']}  성공: {summary['n_ok']}  "
        f"실패: {summary['n_failed']}  소요: {summary['duration_seconds']:.1f}s"
    )

    for workload_summary in (summary["chat"], summary[summary["antagonist_workload"]]):
        tag = workload_summary["workload"]
        if workload_summary["n_ok"] == 0:
            print(f"\n  [{tag.upper()}] 성공한 요청 없음 (실패: {workload_summary['n_failed']})")
            continue

        print(
            f"\n  [{tag.upper()}]  성공: {workload_summary['n_ok']}  "
            f"실패: {workload_summary['n_failed']}"
        )
        print(
            f"  처리량: {workload_summary['throughput_req_per_s']:.2f} req/s  "
            f"{workload_summary['throughput_tok_per_s']:.1f} tok/s"
        )
        print(
            f"  SLO attainment (TTFT ≤ {workload_summary['slo_ms']:.0f}ms): "
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


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    antagonist_config = resolve_antagonist_config(args)
    antagonist_tag = antagonist_config["tag"]
    antagonist_qps = antagonist_config["qps"]
    antagonist_num_prompts = antagonist_config["num_prompts"]
    antagonist_slo_ms = antagonist_config["slo_ms"]

    chat_trace_path = (
        args.chat_trace
        if args.chat_trace is not None
        else args.workloads_root / "sharegpt/sharegpt_victim_100conv_10turn.jsonl"
    )
    chat_trace = resolve_trace_path(
        chat_trace_path,
        "Chat",
        "  cd workloads/sharegpt && python build_sharegpt_workload.py "
        "--output sharegpt_victim_100conv_10turn.jsonl",
    )
    antagonist_trace = resolve_trace_path(
        antagonist_config["trace"],
        antagonist_config["label"],
        antagonist_config["build_hint"],
    )
    output_path = resolve_output_path(args, antagonist_tag)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chat_requests = load_jsonl(chat_trace)
    antagonist_requests = load_jsonl(antagonist_trace)
    if args.num_chat_prompts > 0:
        chat_requests = chat_requests[: args.num_chat_prompts]
    if antagonist_num_prompts > 0:
        antagonist_requests = antagonist_requests[:antagonist_num_prompts]

    total = len(chat_requests) + len(antagonist_requests)
    print(
        f"quota_mode={args.quota_mode} | chat={len(chat_requests)} @ {args.chat_qps} qps | "
        f"{antagonist_tag}={len(antagonist_requests)} @ {antagonist_qps} qps | "
        f"total={total} | concurrency={args.max_concurrency} | url={args.url}"
    )
    print(f"chat_trace={chat_trace}")
    print(f"{antagonist_tag}_trace={antagonist_trace}")
    print(f"output={output_path}")

    queue: asyncio.Queue = asyncio.Queue()
    semaphore = asyncio.Semaphore(args.max_concurrency)
    results: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=args.timeout_seconds)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        start_perf = time.perf_counter()
        with tqdm(total=total, unit="req", dynamic_ncols=True) as pbar:
            # 소비자 풀: max_concurrency 개를 미리 띄워두고 큐를 비운다.
            consumer_tasks = [
                asyncio.create_task(
                    consumer(
                        queue=queue,
                        session=session,
                        url=args.url,
                        model=args.model,
                        semaphore=semaphore,
                        results=results,
                        progress_bar=pbar,
                    )
                )
                for _ in range(args.max_concurrency)
            ]

            # 두 workload의 Poisson 도착을 독립적으로 동시에 진행한다(seed 42/43으로
            # 재현 가능). gather가 끝나면 모든 요청이 큐에 들어간 상태다.
            await asyncio.gather(
                workload_producer(chat_requests, args.chat_qps, "chat", queue, seed=42),
                workload_producer(antagonist_requests, antagonist_qps, antagonist_tag, queue, seed=43),
            )
            # 두 producer가 모두 끝난 뒤 sentinel(None)을 하나만 넣는다. consumer가
            # 릴레이로 재투입하므로 풀 전체가 순차 종료된다(consumer 주석 참고).
            await queue.put(None)
            await asyncio.gather(*consumer_tasks)

        # 전체 소요 시간(throughput 계산의 분모).
        duration_seconds = time.perf_counter() - start_perf

    summary = build_summary(
        results,
        duration_seconds,
        args.chat_slo_ms,
        antagonist_tag,
        antagonist_slo_ms,
        args.quota_mode,
    )
    summary_path = resolve_summary_path(output_path)

    write_jsonl(output_path, results)
    write_json(summary_path, summary)
    print_summary(summary)
    print(f"\n저장 완료: {output_path}")
    print(f"요약 저장 완료: {summary_path}")


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

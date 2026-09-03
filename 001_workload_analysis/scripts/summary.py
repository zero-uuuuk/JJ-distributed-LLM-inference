"""vLLM 측정 결과를 workload별 summary로 집계하고 저장한다."""

import json
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# 결과 집계
# ---------------------------------------------------------------------------


def summarize_results(
    workload: str,
    results: list[dict[str, Any]],
    schedule_config: dict[str, Any],
) -> dict[str, Any]:
    """workload별 요청·prompt·cache·latency summary를 생성한다."""

    # 성공 요청만 metric 집계 대상으로 선택
    successful = [result for result in results if result["status"] == "ok"]

    # metric별 통계 결과 초기화
    metrics: dict[str, dict[str, float | None]] = {}
    for key in (
        "vllm_prompt_tokens",
        "cached_tokens",
        "cache_hit_rate",
        "ttft_ms",
        "e2e_ms",
    ):

        # 현재 metric의 유효값을 NumPy 배열로 변환
        values = np.asarray(
            [result[key] for result in successful if result[key] is not None],
            dtype=float,
        )

        # 값이 없는 metric의 기본 summary 구성
        summary: dict[str, float | None] = {
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
        }

        if values.size:
            # 평균·p50·p95·p99 통계 계산
            p50, p95, p99 = np.percentile(values, [50, 95, 99])
            summary.update(
                mean=float(np.mean(values)),
                p50=float(p50),
                p95=float(p95),
                p99=float(p99),
            )

        # metric별 summary 저장
        metrics[key] = summary

    # workload 결과·스케줄 metadata 병합
    return {
        "workload": workload,
        "n_total": len(results),
        "n_ok": len(successful),
        "n_failed": len(results) - len(successful),
        "vllm_prompt_tokens": metrics["vllm_prompt_tokens"],
        "cached_tokens": metrics["cached_tokens"],
        "cache_hit_rate": metrics["cache_hit_rate"],
        "ttft_ms": metrics["ttft_ms"],
        "e2e_ms": metrics["e2e_ms"],
        "schedule": {
            "type": schedule_config["type"],
            "qps": schedule_config["qps"],
            "seed": schedule_config.get("seed"),
            "max_concurrency": schedule_config["max_concurrency"],
        },
    }


# ---------------------------------------------------------------------------
# summary 저장
# ---------------------------------------------------------------------------


def save_summary(
    workload: str,
    summary: dict[str, Any],
    config: dict[str, Any],
    base_dir: Path,
) -> Path:
    """집계 summary를 config의 output directory에 JSON으로 저장한다."""
    # 결과 디렉터리 생성
    output_dir = base_dir / config["vllm"]["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    # workload summary JSON 저장
    summary_path = output_dir / f"{workload}_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary_path

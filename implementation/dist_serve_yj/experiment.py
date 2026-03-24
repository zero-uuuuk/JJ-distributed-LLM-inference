"""
experiment.py
실험 진입점.

Phase 1 : 기능 검증  — B=[1,2,4],    L=[128,512,1024], iter=8
Phase 2 : 본 실험   — B=[1,2,4,8],  L=[128,256,512,1024,1536], iter=20
                     B=8, L=2048 추가
Analysis : Phase 2 완료 후 동일 데이터에서 두 축 분석
    - Lm 탐색  : B 고정 → L 증가별 throughput  (phase2_lm_view.json)
    - 배치 스케일링 : L 고정 → B 증가별 throughput  (phase2_batch_scaling.json)

각 (B, L) 조합에 대해:
    1. seed=42 고정 입력 텐서 생성
    2. engine.warmup() 실행 (측정 제외)
    3. GPUMonitor 시작
    4. prefill_step() × n_iters 반복 측정
    5. GPUMonitor 중지 + 집계
    6. summarize() 로 latency/throughput 계산
    7. results/ 에 JSON 저장

OOM 발생 시 해당 조합을 실패로 기록하고 다음 조합 진행.
"""

import json
import os
import time
import argparse

import torch

from engine import PrefillEngine
from measure import GPUMonitor, summarize


# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
MODEL_NAME = "facebook/opt-1.3b"
SEED = 42
WARMUP_ITERS = 3
VOCAB_SIZE = 50272  # OPT-6.7B vocabulary size

PHASE1_BATCH_SIZES = [1, 2, 4]
PHASE1_SEQ_LENS = [128, 512, 1024]
PHASE1_ITERS = 8

PHASE2_BATCH_SIZES = [1, 2, 4, 8]
PHASE2_SEQ_LENS = [128, 256, 512, 1024, 1536]
PHASE2_EXTRA = [(8, 2048)]          # 추가 조합
PHASE2_ITERS = 20


# ---------------------------------------------------------------------------
# 입력 생성 헬퍼
# ---------------------------------------------------------------------------

def make_input_ids(batch_size: int, seq_len: int, device: str = "cuda") -> torch.Tensor:
    """
    seed=42 고정 랜덤 토큰으로 [B, L] 텐서를 생성한다.
    동일한 (B, L)에 대해 항상 동일한 텐서를 반환하므로
    run-to-run 입력 분포 변동이 없다.
    """
    gen = torch.Generator()
    gen.manual_seed(SEED)
    ids = torch.randint(0, VOCAB_SIZE, (batch_size, seq_len), generator=gen)
    return ids.to(device)


# ---------------------------------------------------------------------------
# 단일 (B, L) 조합 실행
# ---------------------------------------------------------------------------

def run_one(
    engine: PrefillEngine,
    batch_size: int,
    seq_len: int,
    n_iters: int,
    gpu_monitor_interval: float = 0.5,
) -> dict:
    """
    단일 (batch_size, seq_len) 조합을 n_iters 회 측정하고 결과 dict를 반환한다.
    OOM 발생 시 {"oom": True} 를 반환한다.

    Returns:
        dict with keys:
            batch_size, seq_len, oom (bool),
            latency stats (from summarize()),
            gpu stats (from GPUMonitor.aggregate())
    """
    print(f"  [run] B={batch_size}, L={seq_len}, iters={n_iters}")

    # --- 입력 준비 (측정 구간 밖) ---
    try:
        input_ids = make_input_ids(batch_size, seq_len)
    except torch.cuda.OutOfMemoryError:
        print(f"  [OOM] 입력 생성 단계: B={batch_size}, L={seq_len}")
        return {"batch_size": batch_size, "seq_len": seq_len, "oom": True}

    # --- 워밍업 (측정 제외) ---
    try:
        engine.warmup(input_ids, n=WARMUP_ITERS)
    except torch.cuda.OutOfMemoryError:
        print(f"  [OOM] 워밍업 단계: B={batch_size}, L={seq_len}")
        torch.cuda.empty_cache()
        return {"batch_size": batch_size, "seq_len": seq_len, "oom": True}

    # --- GPU 모니터 시작 ---
    monitor = GPUMonitor(interval=gpu_monitor_interval)
    monitor.start()

    # --- 반복 측정 ---
    latencies = []
    oom_hit = False
    for i in range(n_iters):
        try:
            lat = engine.prefill_step(input_ids)
            latencies.append(lat)
        except torch.cuda.OutOfMemoryError:
            print(f"  [OOM] iteration {i}: B={batch_size}, L={seq_len}")
            oom_hit = True
            break

    # --- GPU 모니터 중지 ---
    monitor.stop()

    if oom_hit or not latencies:
        torch.cuda.empty_cache()
        return {"batch_size": batch_size, "seq_len": seq_len, "oom": True}

    # --- 집계 ---
    stats = summarize(latencies, batch_size, seq_len)
    gpu_stats = monitor.aggregate()

    result = {
        "batch_size": batch_size,
        "seq_len": seq_len,
        "oom": False,
        **stats,
        **gpu_stats,
    }

    print(
        f"    → mean_lat={stats['mean_latency_sec']:.4f}s  "
        f"throughput={stats['throughput_tokens_per_sec']:.0f} tok/s  "
        f"gpu_util={gpu_stats.get('gpu_util_mean_pct', 'N/A')}%"
    )
    return result


# ---------------------------------------------------------------------------
# 결과 저장
# ---------------------------------------------------------------------------

def save_results(results: list, filename: str) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"[save] 결과 저장 완료: {path}")


# ---------------------------------------------------------------------------
# Phase 1 : 기능 검증
# ---------------------------------------------------------------------------

def run_phase1(engine: PrefillEngine) -> None:
    """
    기능 검증 단계.

    목적:
        - 코드 실행 정상 동작 확인
        - 고정 시드 입력 생성 확인
        - latency / throughput / nvidia-smi 로그 저장 확인

    범위: B=[1,2,4], L=[128,512,1024], iter=8
    """
    print("\n========== Phase 1: 기능 검증 ==========")
    results = []

    for B in PHASE1_BATCH_SIZES:
        for L in PHASE1_SEQ_LENS:
            result = run_one(engine, B, L, PHASE1_ITERS)
            results.append(result)

    save_results(results, "phase1_sanity.json")
    print("========== Phase 1 완료 ==========\n")


# ---------------------------------------------------------------------------
# Phase 2 : 본 실험
# ---------------------------------------------------------------------------

def run_phase2(engine: PrefillEngine) -> None:
    """
    본 실험 단계.

    범위:
        B=[1,2,4,8], L=[128,256,512,1024,1536], iter=20
        + B=8, L=2048

    각 (B,L) 조합마다:
        고정 시드 입력 → 워밍업 → 반복 측정 → GPU 집계 → JSON 저장

    OOM 발생 시 해당 조합을 실패(oom=True)로 기록하고 다음 조합 진행.
    """
    print("\n========== Phase 2: 본 실험 ==========")
    results = []

    # 기본 grid
    combos = [
        (B, L)
        for B in PHASE2_BATCH_SIZES
        for L in PHASE2_SEQ_LENS
    ]
    # 추가 조합
    combos += PHASE2_EXTRA

    for B, L in combos:
        result = run_one(engine, B, L, PHASE2_ITERS)
        results.append(result)
        # 조합 완료마다 즉시 저장 (Spot 중단 대응)
        save_results(results, "phase2_grid.json")

    # OOM 목록 별도 저장
    oom_records = [r for r in results if r.get("oom")]
    save_results(oom_records, "phase2_oom_log.json")

    # 사후 분석 (추가 GPU 연산 없음, 데이터 재구성만)
    analyze_results(results)

    print("========== Phase 2 완료 ==========\n")


# ---------------------------------------------------------------------------
# 사후 분석 (Phase 2 데이터 재사용)
# ---------------------------------------------------------------------------

def analyze_results(results: list) -> None:
    """
    Phase 2 grid 결과를 두 가지 축으로 재구성하여 JSON 저장한다.
    추가 GPU 연산은 없고 수집된 데이터를 재정렬하는 것뿐이다.

    1) Lm 탐색 view  : B 고정 → L 증가별 throughput
       → phase2_lm_view.json
       [
         { "batch_size": 1, "by_seq_len": [
             {"seq_len": 128, "throughput_tokens_per_sec": ..., "mean_latency_sec": ..., "gpu_util_mean_pct": ...},
             ...
           ]
         }, ...
       ]

    2) 배치 스케일링 view : L 고정 → B 증가별 throughput
       → phase2_batch_scaling.json
       [
         { "seq_len": 128, "by_batch_size": [
             {"batch_size": 1, "throughput_tokens_per_sec": ..., "mean_latency_sec": ..., "gpu_util_mean_pct": ...},
             ...
           ]
         }, ...
       ]
    """
    # 성공 레코드만 사용
    ok = [r for r in results if not r.get("oom")]

    def _pick(r: dict) -> dict:
        """분석에 필요한 핵심 지표만 추출."""
        return {
            "throughput_tokens_per_sec": r["throughput_tokens_per_sec"],
            "mean_latency_sec": r["mean_latency_sec"],
            "std_latency_sec": r["std_latency_sec"],
            "gpu_util_mean_pct": r.get("gpu_util_mean_pct"),
            "gpu_util_max_pct": r.get("gpu_util_max_pct"),
            "gpu_mem_max_mb": r.get("gpu_mem_max_mb"),
        }

    # ── 1) Lm 탐색 view : B 고정, L 증가 ────────────────────────────────
    batch_sizes = sorted({r["batch_size"] for r in ok})
    lm_view = []
    for B in batch_sizes:
        rows = sorted(
            [r for r in ok if r["batch_size"] == B],
            key=lambda r: r["seq_len"],
        )
        lm_view.append({
            "batch_size": B,
            "by_seq_len": [
                {"seq_len": r["seq_len"], **_pick(r)} for r in rows
            ],
        })
    save_results(lm_view, "phase2_lm_view.json")

    # ── 2) 배치 스케일링 view : L 고정, B 증가 ──────────────────────────
    seq_lens = sorted({r["seq_len"] for r in ok})
    batch_scaling = []
    for L in seq_lens:
        rows = sorted(
            [r for r in ok if r["seq_len"] == L],
            key=lambda r: r["batch_size"],
        )
        batch_scaling.append({
            "seq_len": L,
            "by_batch_size": [
                {"batch_size": r["batch_size"], **_pick(r)} for r in rows
            ],
        })
    save_results(batch_scaling, "phase2_batch_scaling.json")

    print("[analyze] Lm view → phase2_lm_view.json")
    print("[analyze] 배치 스케일링 view → phase2_batch_scaling.json")


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prefill Compute-Bound 탐색 실험 (dist_serve_yj)"
    )
    parser.add_argument(
        "--phase",
        choices=["1", "2", "all"],
        default="all",
        help="실행할 실험 단계 (기본: all)",
    )
    parser.add_argument(
        "--model",
        default=MODEL_NAME,
        help=f"Hugging Face 모델 ID 또는 로컬 경로 (기본: {MODEL_NAME})",
    )
    parser.add_argument(
        "--no-8bit",
        action="store_true",
        help="8-bit 양자화 비활성화 (fp16 로드)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # GPU 필수 확인
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU를 찾을 수 없습니다. GPU 인스턴스에서 실행하세요.")

    print(f"[main] GPU: {torch.cuda.get_device_name(0)}")
    print(f"[main] VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    engine = PrefillEngine(
        model_name=args.model,
        load_in_8bit=not args.no_8bit,
    )

    if args.phase in ("1", "all"):
        run_phase1(engine)

    if args.phase in ("2", "all"):
        run_phase2(engine)

    print("[main] 모든 실험 완료.")


if __name__ == "__main__":
    main()

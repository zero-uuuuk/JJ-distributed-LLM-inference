"""
experiment.py
-------------
Prefill-Decoding Interference 측정 실험 진입점.

실험 조건:
  - Baseline   : decoding N개, iter 0~199
  - Baseline+1 : decoding N+1개, iter 0~199  (N=8, 16만 적용)
  - Treatment  : iter 0에서 decoding N개 + prefill 1개 동시 실행,
                 iter 1~199는 decoding N+1개

시퀀스 관리:
  요청별 KV Cache를 리스트로 관리한다.
  매 iteration마다 run_iteration()이 갱신된 KV Cache를 반환하며,
  다음 iteration에 재사용한다. input_ids는 항상 next token 1개만 전달한다.

실행:
  python experiment.py
"""

import json
import os
import torch
import numpy as np
from engine import OPTEngine
from measure import IterTimer, summarize

# -----------------------------------------------------------------------
# 실험 파라미터
# -----------------------------------------------------------------------
MODEL_NAME = "facebook/opt-1.3b"
DEVICE = "cuda"
SEED = 42

DECODE_INPUT_LEN = 256    # decoding 요청 초기 입력 토큰 수
DECODE_OUTPUT_LEN = 200   # decoding 출력 토큰 수 (= iteration 수)
PREFILL_INPUT_LEN = 512   # prefill 요청 입력 토큰 수
N_RUNS = 10

BATCH_SIZES = [8, 16, 32]
BASELINE_PLUS1_BATCH_SIZES = [8, 16]  # N+1=33은 OOM 위험으로 제외

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


# -----------------------------------------------------------------------
# 데이터 생성
# -----------------------------------------------------------------------

def make_seq(seq_len: int, vocab_size: int, seed: int) -> torch.Tensor:
    """재현 가능한 random token 시퀀스 생성. shape: (seq_len,)"""
    rng = torch.Generator()
    rng.manual_seed(seed)
    return torch.randint(0, vocab_size, (seq_len,), generator=rng)



# -----------------------------------------------------------------------
# 실험 조건별 실행 함수
# -----------------------------------------------------------------------

def run_baseline(engine: OPTEngine, batch_size: int, n_runs: int) -> list[np.ndarray]:
    """
    [Baseline] decoding N개, iter 0~199.
    순수 decoding 배치의 TPOT 기준값을 측정한다.
    """
    tpot_runs = []

    for run in range(n_runs):
        # 준비 단계: 각 요청을 prefill하여 KV Cache 확보 (측정 제외)
        input_ids_list = [
            make_seq(DECODE_INPUT_LEN, engine.vocab_size, seed=SEED + i)
            for i in range(batch_size)
        ]
        kv_list, next_token_list = engine.prefill(input_ids_list)

        timer = IterTimer(DECODE_OUTPUT_LEN)

        for _ in range(DECODE_OUTPUT_LEN):
            timer.start()
            next_token_list, kv_list, _, _ = engine.run_iteration(next_token_list, kv_list)
            torch.cuda.synchronize()
            timer.stop()

        tpot_runs.append(timer.tpot)
        print(f"  [Baseline] batch={batch_size} run={run+1}/{n_runs} done")

    return tpot_runs


def run_baseline_plus1(engine: OPTEngine, batch_size: int, n_runs: int) -> list[np.ndarray]:
    """
    [Baseline+1] decoding N+1개, iter 0~199.
    배치 크기 증가 효과를 분리하기 위한 대조군.

    N+1번째 요청은 Treatment의 prefill 요청과 동일한 입력(SEED+100)을 사용하되,
    준비 단계에서 미리 prefill 완료된 상태로 시작한다.
    이렇게 해야 Treatment[iter 0] - Baseline+1[iter 0]에서
    입력 차이로 인한 오차 없이 순수 prefill 간섭 효과만 추출할 수 있다.
    """
    tpot_runs = []

    for run in range(n_runs):
        # N개의 기본 decoding 요청 (Baseline과 동일한 seed)
        input_ids_list = [
            make_seq(DECODE_INPUT_LEN, engine.vocab_size, seed=SEED + i)
            for i in range(batch_size)
        ]
        # N+1번째 요청: Treatment의 prefill 요청과 동일한 seed(SEED+100), 동일한 길이
        extra_seq = make_seq(DECODE_INPUT_LEN, engine.vocab_size, seed=SEED + 100)
        input_ids_list.append(extra_seq)

        kv_list, next_token_list = engine.prefill(input_ids_list)

        timer = IterTimer(DECODE_OUTPUT_LEN)

        for _ in range(DECODE_OUTPUT_LEN):
            timer.start()
            next_token_list, kv_list, _, _ = engine.run_iteration(next_token_list, kv_list)
            torch.cuda.synchronize()
            timer.stop()

        tpot_runs.append(timer.tpot)
        print(f"  [Baseline+1] batch={batch_size} run={run+1}/{n_runs} done")

    return tpot_runs


def run_treatment(engine: OPTEngine, batch_size: int, n_runs: int) -> list[np.ndarray]:
    """
    [Treatment]
      iter 0   : decoding N개 + prefill 1개를 하나의 forward pass로 실행 (간섭 발생)
      iter 1~  : prefill 완료 후 decoding N+1개로 전환

    iter 0의 TPOT spike가 prefill-decoding co-location interference를 나타낸다.
    """
    tpot_runs = []

    for run in range(n_runs):
        # 준비 단계: decoding 배치 KV Cache 확보
        input_ids_list = [
            make_seq(DECODE_INPUT_LEN, engine.vocab_size, seed=SEED + i)
            for i in range(batch_size)
        ]
        kv_list, next_token_list = engine.prefill(input_ids_list)

        # prefill 요청 시퀀스 (decoding 요청과 seed 범위를 분리)
        prefill_seq = make_seq(PREFILL_INPUT_LEN, engine.vocab_size, seed=SEED + 100)

        timer = IterTimer(DECODE_OUTPUT_LEN)

        for iteration in range(DECODE_OUTPUT_LEN):
            timer.start()

            if iteration == 0:
                # iter 0: decoding N개 + prefill 1개를 단일 forward pass로 실행
                # prefill의 compute-bound 연산이 decoding TPOT에 간섭을 유발하는 지점
                next_token_list, kv_list, prefill_kv, prefill_next_token = engine.run_iteration(
                    next_token_list, kv_list, prefill_seq=prefill_seq
                )
                torch.cuda.synchronize()
                # prefill 완료 → 신규 요청을 decoding 배치에 추가 (iter 1부터 N+1개)
                # prefill_next_token은 모델 출력 토큰 (입력의 마지막 토큰이 아님)
                next_token_list = next_token_list + [prefill_next_token]
                kv_list = kv_list + [prefill_kv]
            else:
                # iter 1~199: decoding N+1개 (prefill 요청도 decoding 단계로 전환됨)
                next_token_list, kv_list, _, _ = engine.run_iteration(next_token_list, kv_list)
                torch.cuda.synchronize()

            timer.stop()

        tpot_runs.append(timer.tpot)
        print(f"  [Treatment] batch={batch_size} run={run+1}/{n_runs} done")

    return tpot_runs


# -----------------------------------------------------------------------
# 결과 저장
# -----------------------------------------------------------------------

def save_results(results: dict, batch_size: int):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"tpot_batch{batch_size}.json")
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  결과 저장: {path}")


# -----------------------------------------------------------------------
# main
# -----------------------------------------------------------------------

def warmup(engine: OPTEngine):
    """
    CUDA kernel 초기화 비용을 측정 구간 밖으로 빼기 위해
    dummy forward pass를 2회 실행한다.
    """
    dummy = make_seq(DECODE_INPUT_LEN, engine.vocab_size, seed=0)
    kv_list, next_token_list = engine.prefill([dummy])
    for _ in range(2):
        next_token_list, kv_list, _, _ = engine.run_iteration(next_token_list, kv_list)


def main():
    print(f"모델 로딩: {MODEL_NAME}")
    engine = OPTEngine(model_name=MODEL_NAME, device=DEVICE)

    print("GPU warm-up 중...")
    warmup(engine)

    for batch_size in BATCH_SIZES:
        print(f"\n=== batch_size={batch_size} ===")
        results = {}

        print("[Baseline]")
        results["baseline"] = summarize(run_baseline(engine, batch_size, N_RUNS))

        if batch_size in BASELINE_PLUS1_BATCH_SIZES:
            print("[Baseline+1]")
            results["baseline_plus1"] = summarize(run_baseline_plus1(engine, batch_size, N_RUNS))

        print("[Treatment]")
        results["treatment"] = summarize(run_treatment(engine, batch_size, N_RUNS))

        save_results(results, batch_size)

    print("\n실험 완료.")


if __name__ == "__main__":
    main()
